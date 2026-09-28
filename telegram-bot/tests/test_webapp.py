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
from test_bot import CF3 as CF_NUOVA, NRE3 as NRE_NUOVA, COSA3  # noqa: E402

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
    post(app, f"/ui/r/{pid}/auto", {"on": "1"})
    assert app.store.get(pid)["auto"] == {"on": True}
    _, _, corpo = get(app, f"/ui/r/{pid}/auto")
    assert 'value="1" checked' in corpo.decode() and "Calendario: tutti i giorni" in corpo.decode()
    post(app, f"/ui/r/{pid}/auto", {"on": "0"})
    assert app.store.get(pid)["auto"] is None
    for cattiva in ({"on": "2"}, {"giorni": "3"}, {}):
        stato, _, _ = post(app, f"/ui/r/{pid}/auto", cattiva)
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
    post(app, f"/ui/r/{p['id']}/auto", {"on": "1"})  # ...intanto l'utente attiva l'automatica
    b.salva(p, "errori", "ultimo")
    assert b.store.get(p["id"])["auto"] == {"on": True}


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
    assert "Prima della tua prenotazione, dove cerchi" in t and "In altre zone" in t and 'value="ASTI"' in t
    p2 = b.store.get(fam["id"])
    p2["storico"].append({"t": time.time() + 60, "a": p2["storico"][0]["a"], "r": p2["storico"][0]["r"]})
    b.store.save(p2)
    _, _, corpo = get(app, f"/ui/r/{fam['id']}/storico")
    t = corpo.decode()
    assert "<svg" in t and "tua prenotazione" in t and "<title>" in t and "Mostra i cambiamenti in tabella" in t


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
    prima = b._attesa
    stato, _, corpo = get(app, "/ui/admin", chat=1)
    assert stato == 200 and "ricette attive" in corpo.decode() and "sessioni" in corpo.decode()
    assert "Attesa di una risposta lenta" in corpo.decode()
    assert b._attesa == prima  # la pagina calcola con il suo database: lo stato del bot non si tocca
    assert get(app, "/ui/admin", chat=2)[0] == 404


def test_scheda_con_prossimo_controllo_e_barra(app):
    _, _, corpo = get(app, "/ui/ricette")
    t = corpo.decode()
    assert "⏭ Prossimo controllo" in t and "🔄 Controlla ora" in t and "📈 Andamento" in t and "✏️ Modifica" in t and "🔒" in t
    assert "Aggiungi una ricetta" in t  # 2 ricette su 3: si puo' aggiungere
    app.bot.max_pratiche = 2
    assert "Aggiungi una ricetta" not in get(app, "/ui/ricette")[2].decode()


def test_date_viste_prenotabili_anche_fuori_area(app):
    b = app.bot
    fam = pratica(b, 1, 1)  # cerca solo ad Alba; Asti e' prima ma fuori area
    b.controlla(fam)
    b.offerte.clear()
    _, _, corpo = get(app, f"/ui/r/{fam['id']}/date")
    t = corpo.decode()
    assert t.count('action="/ui/r/') >= 2 and "fuori dalla zona in cui cerchi" in t and "PRIMA della data attuale" in t
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
    assert "🔄 Controlla ora" in corpo.decode() and 'action="/ui/r/' not in corpo.decode()
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


def test_metriche_sopravvivono_al_riavvio(app):
    b = app.bot
    b.admin = "1"
    b.portale(lambda: None)
    b.metriche.clear()  # come dopo un riavvio: la memoria si svuota, il database no
    _, _, corpo = get(app, "/ui/admin", chat=1)
    t = corpo.decode()
    n = app.store.db.execute("SELECT COUNT(*) FROM metriche").fetchone()[0]
    assert n >= 1 and "Dati dal" in t and f"<strong>{n}</strong><span>sessioni</span>" in t


def test_file_statici_con_versione(app):
    _, _, corpo = app.gestisci("GET", "/", {})
    t = corpo.decode()
    v = webapp.versione_statico("app.css")
    assert f"/static/app.css?v={v}" in t and "/static/app.js?v=" in t and "/static/htmx.min.js?v=" in t
    stato, h, _ = app.gestisci("GET", "/static/app.css", {})  # il gestore toglie ?v= prima di arrivare qui
    assert stato == 200 and "immutable" in h["Cache-Control"]


def test_grafico_senza_scritte_nell_area_dei_dati(app):
    """Le scritte del grafico stanno solo sugli assi: linea e punti non possono coprirle."""
    b = app.bot
    p = pratica(b, 1, 0)
    rif = botmod.attuale_di(p).quando
    ora = time.time()
    for giorni in ((-400, 30), (-3, 5), (0, 0)):  # anni diversi, pochi giorni, sempre uguale alla prenotazione
        p["storico"] = [{"t": ora - 3600 * (5 - i), "a": (rif + botmod.timedelta(days=g)).isoformat(), "r": rif.isoformat()}
                        for i, g in enumerate(giorni * 3)]
        b.store.save(p)
        t = get(app, f"/ui/r/{p['id']}/storico")[2].decode()
        svg = t[t.index("<svg"):t.index("</svg>")]
        assert 'class="etichetta' not in svg and f"{rif:%d/%m/%y}" in svg and 'class="punto ultimo"' in svg
        assert "la tua prenotazione" in t


def test_controlla_ora_rifiutato_subito_se_troppo_presto_o_con_offerta(app):
    """L'app non dice "controllo avviato" quando il bot lo rifiuterebbe: lo dice subito, con l'ora."""
    b = app.bot
    p = pratica(b, 1, 0)
    p["ultimo"] = {"ts": time.time() - 60}
    b.store.save(p)
    stato, _, corpo = post(app, f"/ui/r/{p['id']}/controlla", {})
    assert stato == 429 and "il prossimo è possibile dalle" in corpo.decode() and not b.coda.qsize()
    b.controlla(b.store.get(p["id"]))  # apre un'offerta
    p = b.store.get(p["id"])
    p["ultimo"] = {"ts": 0}
    b.store.save(p)
    stato, _, corpo = post(app, f"/ui/r/{p['id']}/controlla", {})
    assert stato == 409 and "offerta aperta" in corpo.decode() and not b.coda.qsize()


def test_grafico_asse_mai_vuoto_e_legenda_onesta(app):
    """Con pochi giorni di escursione l'asse ha comunque almeno due date oltre alla prenotazione; se l'ultimo
    controllo non ha trovato date, nessun punto viene spacciato per "ultimo controllo"."""
    from datetime import datetime
    rif = datetime(2027, 1, 21, 13, 0)
    ora = time.time()
    punti = [(ora - 3600, datetime(2027, 1, 17, 9, 0)), (ora - 1800, datetime(2027, 1, 26, 9, 0)), (ora, None)]
    svg = app.grafico_storico(punti, rif)
    assert svg.count('class="asse"') >= 2 + 2  # 2 date del tempo + almeno 2 date sull'asse verticale
    assert 'class="punto ultimo"' not in svg and "non ha trovato date" in svg
    svg = app.grafico_storico(punti[:2], rif)
    assert 'class="punto ultimo"' in svg and "ultimo controllo" in svg


# --- ricetta mai prenotata ------------------------------------------------------------------
def test_ricetta_mai_prenotata_dall_app(app):
    (stato, _, corpo), _ = cerca_e_attendi(app, 3, {"cf": CF_NUOVA, "nre": NRE_NUOVA, "consenso": "1"})
    t = corpo.decode()
    assert stato == 200 and "non è ancora prenotata" in t and COSA3 in t.upper()
    assert 'value="tutte"' in t and 'value="altro"' in t and "Solo in questa sede" not in t and "2100" not in t
    [p] = app.store.della_chat(3)
    assert botmod.da_prenotare(p)
    assert post(app, f"/ui/r/{p['id']}/dove", {"tipo": "sede"}, chat=3)[0] == 400  # nessuna sede di riferimento
    post(app, f"/ui/r/{p['id']}/dove", {"tipo": "tutte"}, chat=3)
    t = get(app, "/ui/ricette", chat=3)[2].decode()
    assert "Da prenotare" in t and "Non ancora prenotata" in t and "2100" not in t and "nessuna (da prenotare)" not in t


def test_ricetta_mai_prenotata_date_dove_e_andamento(app):
    b = app.bot
    (stato, _, _), _ = cerca_e_attendi(app, 3, {"cf": CF_NUOVA, "nre": NRE_NUOVA, "consenso": "1"})
    [p] = app.store.della_chat(3)
    post(app, f"/ui/r/{p['id']}/dove", {"tipo": "tutte"}, chat=3)
    b.controlla(b.store.get(p["id"]))
    b.offerte.clear()
    t = get(app, f"/ui/r/{p['id']}/date", chat=3)[2].decode()
    visibile = __import__("re").sub(r'value="[^"]*"', "", t)  # la data di riferimento nascosta non conta
    assert "✅ Dove cerchi" in t and "Prima della tua prenotazione" not in t and "Prenoto " in t and "2100" not in visibile
    t = get(app, f"/ui/r/{p['id']}/dove", chat=3)[2].decode()
    assert 'value="provincia_vista"' in t and 'value="sede_vista"' in t
    assert post(app, f"/ui/r/{p['id']}/dove", {"tipo": "provincia_vista", "prov": "MI"}, chat=3)[0] == 400
    post(app, f"/ui/r/{p['id']}/dove", {"tipo": "provincia_vista", "prov": "TO"}, chat=3)
    assert app.store.get(p["id"])["zona"] == {"tipo": "provincia", "valore": "TO"}
    q = b.store.get(p["id"])
    q["storico"].append({**q["storico"][0], "t": time.time() + 60})
    b.store.save(q)
    t = get(app, f"/ui/r/{p['id']}/storico", chat=3)[2].decode()
    assert "Non ancora prenotata" in t and "la tua prenotazione" not in t and "2100" not in t and "<svg" in t
    assert 'class="riferimento"' not in t


def giorni_da_oggi(*n):
    oggi = botmod.adesso().date()
    return [(oggi + botmod.timedelta(days=i)).isoformat() for i in n]


def test_foglio_calendario_mesi_segni_e_passati(app):
    b = app.bot
    p = pratica(b, 1)
    oggi = botmod.adesso().date()
    trovata, altra = giorni_da_oggi(3, 5)
    p["viste"] = [{"q": trovata + "T09:00:00", "ok": True}, {"q": altra + "T10:00:00", "ok": False}]
    b.store.save(p)
    stato, _, corpo = get(app, f"/ui/r/{p['id']}/calendario")
    t = corpo.decode()
    assert stato == 200 and t.count('class="cal-mese"') == webapp.MESI_CAL + 1
    assert t.count(" hidden>") == webapp.MESI_CAL  # si vede solo il mese corrente
    assert t.count('class="cal-sett"') == 7 * (webapp.MESI_CAL + 1) and ">lun</button>" in t
    assert f'data-oggi="{oggi.isoformat()}"' in t and f'data-d="{oggi.isoformat()}"' in t
    assert f'class="g si oggi" data-d="{oggi.isoformat()}"' in t
    lunedi = oggi - botmod.timedelta(days=oggi.weekday())
    if lunedi.month == oggi.month and lunedi < oggi:  # i giorni passati della settimana non si toccano
        assert f'<span class="g passato" aria-label="{lunedi:%d/%m}' in t
    prima = lunedi - botmod.timedelta(days=1)
    if prima.month == oggi.month:  # le settimane gia' passate del tutto non si mostrano
        assert f'aria-label="{prima:%d/%m}' not in t
    att = botmod.attuale_di(p).quando.date()
    assert f'data-d="{att.isoformat()}"' in t and "📌" in t  # la prenotazione, a 200 giorni: nei 12 mesi
    assert f'data-d="{trovata}"' in t and 'class="vista buona"' in t and 'class="vista"' in t
    assert 'name="no" value=""' in t and 'name="no_settimana" value=""' in t and "Tutti sì" in t
    _, _, corpo = get(app, "/ui/ricette")
    assert "📅 Calendario" in corpo.decode() and "tutti i giorni" in corpo.decode()


def test_foglio_calendario_salva_giorni_settimana_no_fino_e_tutti_si(app):
    pid = pratica(app.bot, 1)["id"]
    oggi = botmod.adesso().date()
    d1, d2, d3, fino = giorni_da_oggi(20, 21, 22, 9)
    ieri = (oggi - botmod.timedelta(days=1)).isoformat()
    sabato = next(d for d in giorni_da_oggi(*range(30, 37)) if botmod.date.fromisoformat(d).weekday() == 5)
    presto = giorni_da_oggi(4)[0]
    # un tocco su tre giorni di fila, sabati e domeniche no, no fino al nono giorno: si salva tutto insieme
    stato, _, corpo = post(app, f"/ui/r/{pid}/calendario",
                           {"no": ",".join([d3, d1, d2, ieri, sabato, presto, d1]), "no_settimana": "6,5",
                            "no_fino": fino, "si_fino": ""})
    k = app.store.get(pid)["calendario"]
    assert stato == 200 and k["no_settimana"] == [5, 6] and k["no_fino"] == fino and k["si_fino"] == ""
    # tolti: il giorno passato, il sabato (gia' no), quello entro "no fino al" e il doppione
    assert k["no"] == sorted({d for d in (d1, d2, d3) if botmod.date.fromisoformat(d).weekday() < 5})
    t = corpo.decode()
    assert "giorni no: sab, dom, fino al" in t
    assert botmod.descr_calendario(app.store.get(pid)).startswith("no: sab, dom, fino al")
    _, _, corpo = get(app, f"/ui/r/{pid}/calendario")
    t = corpo.decode()
    assert 'name="no_settimana" value="5,6"' in t and f'name="no_fino" value="{fino}"' in t
    assert f'class="g no" data-d="{k["no"][0]}"' in t and 'class="cal-sett no"' in t
    assert f'class="g no fisso" data-d="{fino}"' in t  # no per "no fino al": non si cambia da solo
    # tutti si'
    stato, _, corpo = post(app, f"/ui/r/{pid}/calendario", {"no": "", "no_settimana": "", "no_fino": "", "si_fino": ""})
    assert stato == 200 and app.store.get(pid)["calendario"] == {} and "tutti i giorni sì" in corpo.decode()
    assert botmod.calendario_di(app.store.get(pid)) is None


def test_foglio_calendario_prende_il_posto_delle_regole_di_prima(app):
    b = app.bot
    p = pratica(b, 1)
    p["auto"] = {"giorni": 3}
    p["giorni_ok"] = {"settimana": [0, 1, 2, 3, 4], "date": [], "fascia": "mattina", "entro": ""}
    b.store.save(p)
    fino = giorni_da_oggi(2)[0]
    _, _, corpo = get(app, f"/ui/r/{p['id']}/calendario")
    t = corpo.decode()
    assert 'name="no_settimana" value="5,6"' in t and f'name="no_fino" value="{fino}"' in t
    _, _, corpo = get(app, "/ui/ricette")
    assert "no: sab, dom, fino al" in corpo.decode() and "sì, nei giorni sì del calendario" in corpo.decode()
    post(app, f"/ui/r/{p['id']}/calendario", {"no": "", "no_settimana": "5,6", "no_fino": fino, "si_fino": ""})
    q = app.store.get(p["id"])
    assert "giorni_ok" not in q and q["auto"] == {"on": True}
    # salvato com'era: resta "solo anticipare" delle regole di prima
    assert q["calendario"] == {"no": [], "no_settimana": [5, 6], "no_fino": fino, "si_fino": "", "solo_prima": True}


def test_automatica_dall_app_tiene_i_giorni_di_prima(app):
    b = app.bot
    p = pratica(b, 1)
    p["auto"] = {"dal": giorni_da_oggi(10)[0]}
    b.store.save(p)
    post(app, f"/ui/r/{p['id']}/auto", {"on": "0"})
    q = app.store.get(p["id"])
    assert q["auto"] is None and q["calendario"]["no_fino"] == giorni_da_oggi(9)[0]


def test_foglio_calendario_validazione(app):
    pid = pratica(app.bot, 1)["id"]
    lontano = giorni_da_oggi(webapp.GIORNI_CAL + 1)[0]
    troppe = ",".join(giorni_da_oggi(*range(1, webapp.MAX_NO_CAL + 2)))
    for cattivi in ({"no": "2026-13-01"}, {"no": "20261001"}, {"no": "domani"}, {"no": "2026-10-01T09:00"},
                    {"no": lontano}, {"no": troppe}, {"no_settimana": "7"}, {"no_settimana": "56"},
                    {"no_settimana": "x"}, {"no_fino": lontano}, {"no_fino": "31/12/2026"}, {"si_fino": "x"}):
        stato, _, corpo = post(app, f"/ui/r/{pid}/calendario", cattivi)
        assert stato == 400 and 'class="errore"' in corpo.decode(), cattivi
    assert "calendario" not in app.store.get(pid)
    stato, _, _ = post(app, f"/ui/r/{pid}/calendario", {"no_settimana": "1"}, chat=2)  # ricetta di un altro
    assert stato == 404 and "calendario" not in app.store.get(pid)
    stato, _, _ = get(app, f"/ui/r/{pid}/calendario", chat=2)
    assert stato == 404
    assert post(app, f"/ui/r/{pid}/giorni", {"g1": "1"})[0] == 404  # il foglio di prima non c'e' piu'
    # la dimensione massima ci sta nel limite del corpo
    tutte = ",".join(giorni_da_oggi(*range(1, webapp.MAX_NO_CAL + 1)))
    assert len(urlencode({"no": tutte, "no_settimana": "0,1,2,3,4,5,6", "no_fino": "", "si_fino": ""})) < webapp.MAX_CORPO


def test_foglio_calendario_escaping(app):
    b = app.bot
    p = pratica(b, 1)
    post(app, f"/ui/r/{p['id']}/nome", {"nome": '<b>"x"</b>'})
    t = get(app, f"/ui/r/{p['id']}/calendario")[2].decode()
    assert "<b>" not in t and "&lt;b&gt;&quot;x&quot;&lt;/b&gt;" in t


def test_foglio_calendario_ricetta_mai_prenotata(b, monkeypatch):
    from test_bot import registra_nuova
    monkeypatch.setattr(webapp, "PAUSA_AZIONI", 0)
    b.token = TOKEN
    registra_nuova(b)
    app = webapp.App(b, b.store)
    pid = pratica(b)["id"]
    t = get(app, f"/ui/r/{pid}/calendario")[2].decode()
    assert "non ancora prenotata" in t and "📌" not in t and "2100" not in t
    post(app, f"/ui/r/{pid}/calendario", {"no_settimana": "2"})
    assert app.store.get(pid)["calendario"] == {"no": [], "no_settimana": [2], "no_fino": "", "si_fino": ""}


def test_calendario_salvato_uguale_resta_solo_anticipo(app):
    pid = pratica(app.bot, 1)["id"]
    p = app.store.get(pid)
    oggi = botmod.adesso().date()
    p["auto"] = {"dal": (oggi + botmod.timedelta(days=5)).isoformat()}  # regola di prima: anticipa soltanto
    app.store.save(p)
    fino = (oggi + botmod.timedelta(days=4)).isoformat()
    post(app, f"/ui/r/{pid}/calendario", {"no": "", "no_settimana": "", "no_fino": fino, "si_fino": ""})
    assert app.store.get(pid)["calendario"] == {"no": [], "no_settimana": [], "no_fino": fino, "si_fino": "",
                                                "solo_prima": True}
    post(app, f"/ui/r/{pid}/calendario", {"no": "", "no_settimana": "6", "no_fino": fino, "si_fino": ""})
    assert "solo_prima" not in app.store.get(pid)["calendario"]  # cambiato dall'utente: regola nuova


# --- zona "alcuni comuni" -----------------------------------------------------------------
def riga_comune(t, nome):
    """La riga di un comune nell'elenco "Questi comuni" (etichetta intera)."""
    import re
    m = re.search(r'<label class="cm"[^>]*><input type="checkbox" value="%s"[^>]*>.*?</label>' % re.escape(nome), t)
    return m.group(0) if m else ""


def test_questi_comuni_salva_valida_e_spiega(app):
    pid = pratica(app.bot, 1)["id"]
    stato, _, corpo = post(app, f"/ui/r/{pid}/dove", {"tipo": "comuni", "comuni": "TORINO,moncalieri, Rivoli,TORINO"})
    assert stato == 200 and app.store.get(pid)["zona"] == {"tipo": "comuni", "valore": ["TORINO", "MONCALIERI", "RIVOLI"]}
    assert "cerco a Torino, Moncalieri, Rivoli" in corpo.decode()
    stato, _, corpo = post(app, f"/ui/r/{pid}/dove", {"tipo": "comuni", "comuni": "TORINO,Paperopoli,<script>"})
    t = corpo.decode()
    assert stato == 400 and "Paperopoli" in t and "&lt;script&gt;" in t and "<script>" not in t
    assert post(app, f"/ui/r/{pid}/dove", {"tipo": "comuni", "comuni": " , "})[0] == 400
    troppi = ",".join(n for n, _ in __import__("cup_http").vicini("Torino", 30)[:31])
    stato, _, corpo = post(app, f"/ui/r/{pid}/dove", {"tipo": "comuni", "comuni": troppi})
    assert stato == 400 and "Al massimo 30 comuni: ne hai scritti 31" in corpo.decode()
    assert post(app, f"/ui/r/{pid}/dove", {"tipo": "comuni", "comuni": "x" * 3000})[0] == 400
    assert app.store.get(pid)["zona"]["valore"] == ["TORINO", "MONCALIERI", "RIVOLI"]  # gli errori non toccano nulla
    _, _, corpo = post(app, f"/ui/r/{pid}/dove", {"tipo": "cintura"})
    assert "cerco a Torino e prima cintura" in corpo.decode()
    assert app.store.get(pid)["zona"] == {"tipo": "comuni", "valore": list(botmod.cup_http.CINTURA_TORINO)}


def test_foglio_dove_comuni_vicini_da_spuntare(app):
    pid = pratica(app.bot, 1)["id"]  # prenotazione a Torino
    t = get(app, f"/ui/r/{pid}/dove")[2].decode()
    assert 'value="comuni">' in t and 'name="comuni" value=""' in t and 'name="centro" value="Torino"' in t
    assert "<small>centro</small>" in riga_comune(t, "TORINO") and "<small>8 km</small>" in riga_comune(t, "MONCALIERI")
    assert " hidden>" in riga_comune(t, "FIANO") and "Mostra fino a 40 km" in t  # 23 km: nascosto
    assert riga_comune(t, "PINEROLO") and not riga_comune(t, "SUSA")  # 34 km si', 50 km no
    assert 'class="cm-preset"' in t and 'value="Torino"' in t  # preset e datalist di tutti i comuni
    assert t.index('value="MONCALIERI"') < t.index('value="RIVOLI"')  # dal piu' vicino
    post(app, f"/ui/r/{pid}/dove", {"tipo": "comuni", "comuni": "SUSA,FIANO,MONCALIERI"})
    t = get(app, f"/ui/r/{pid}/dove")[2].decode()
    assert 'value="comuni" checked' in t and 'name="comuni" value="SUSA,FIANO,MONCALIERI"' in t
    assert " checked" in riga_comune(t, "SUSA") and "50 km" in riga_comune(t, "SUSA")  # lontano ma scelto: c'e'
    assert " hidden" not in riga_comune(t, "FIANO") and " checked" in riga_comune(t, "FIANO")
    assert " checked" not in riga_comune(t, "TORINO")


def test_foglio_dove_centra_qui_tiene_le_spunte(app):
    b = app.bot
    fam = pratica(b, 1, 1)  # prenotazione ad Asti, sedi viste anche ad Alba
    b.controlla(fam)
    t = get(app, f"/ui/r/{fam['id']}/dove")[2].decode()
    assert 'name="centro" value="Asti"' in t and "🏥 Alba" in riga_comune(t, "ALBA") and "🏥 = sedi già viste" in t
    assert 'class="cm-preset"' not in t  # Torino e la cintura non sono vicine ad Asti
    t = get(app, f"/ui/r/{fam['id']}/dove?centro=torino&comuni=ALBA%2CRIVOLI&tipo=sede")[2].decode()
    assert 'name="centro" value="Torino"' in t and 'value="comuni" checked' in t
    assert " checked" in riga_comune(t, "RIVOLI") and " checked" in riga_comune(t, "ALBA")
    t = get(app, f"/ui/r/{fam['id']}/dove?centro=%3Cb%3EPaperopoli&comuni=")[2].decode()
    assert "Non trovo «&lt;b&gt;Paperopoli» tra i comuni del Piemonte: centro su Asti." in t and "<b>" not in t


def test_foglio_dove_comuni_ricetta_mai_prenotata(b, monkeypatch):
    from test_bot import registra_nuova
    monkeypatch.setattr(webapp, "PAUSA_AZIONI", 0)
    b.token = TOKEN
    registra_nuova(b)
    app = webapp.App(b, b.store)
    pid = pratica(b)["id"]
    t = get(app, f"/ui/r/{pid}/dove")[2].decode()
    assert 'name="centro" value="Torino"' in t and 'class="cm-preset"' in t and riga_comune(t, "MONCALIERI")
    stato, _, corpo = post(app, f"/ui/r/{pid}/dove", {"tipo": "comuni", "comuni": "TORINO,MONCALIERI"})
    assert stato == 200 and "cerco a Torino, Moncalieri" in corpo.decode()
    b.controlla(b.store.get(pid))
    assert b.zone_viste[-1] == {"tipo": "comuni", "valore": ["TORINO", "MONCALIERI"]}
    assert "a Torino, Moncalieri" in get(app, "/ui/ricette")[2].decode()
