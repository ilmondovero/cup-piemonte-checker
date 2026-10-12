"""Sedi escluse: il filtro delle zone, i pulsanti nei messaggi con le date, /escluse e il foglio della Mini App."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot as botmod  # noqa: E402
import cup_http as c  # noqa: E402
import webapp  # noqa: E402
from test_bot import ATT, MEGLIO, VICINO, b, cq, inviati, msg, pratica, pulsanti, registra, tutti_pulsanti  # noqa: E402,F401
from test_webapp import app, get, post  # noqa: E402,F401

TORINO_A = {"sede": "OSPEDALE A", "comune": "TORINO"}


def cerca_a_torino(b):
    """La zona e' il comune di Torino: escludere OSPEDALE A (Torino) lascia altre sedi possibili."""
    p = pratica(b)
    p["zona"] = {"tipo": "comune", "valore": "TORINO"}
    b.salva(p, "zona")


def test_il_filtro_delle_zone_toglie_le_sedi_escluse_con_qualunque_zona():
    z = c.zona_norm({"tipo": "tutte", "valore": "", "escludi": [TORINO_A, TORINO_A, {"sede": "", "comune": "X"}, "no"]})
    assert z == {"tipo": "tutte", "valore": "", "escludi": [TORINO_A]}  # doppioni e voci illeggibili tolti
    assert "escludi" not in c.zona_norm({"tipo": "tutte", "valore": "", "escludi": []})
    assert "escludi" not in c.zona_norm("tutte")
    for zona in ({"tipo": "tutte", "valore": ""}, {"tipo": "comune", "valore": "TORINO"},
                 {"tipo": "comuni", "valore": ["TORINO", "MONCALIERI"]}):
        assert c.ammesso(MEGLIO, ATT, zona)
        assert not c.ammesso(MEGLIO, ATT, {**zona, "escludi": [TORINO_A]})  # OSPEDALE A, Torino
        assert c.ammesso(VICINO, ATT, {**zona, "escludi": [TORINO_A]}) == c.ammesso(VICINO, ATT, zona)
    # una sede omonima in un altro comune non e' quella esclusa
    assert c.ammesso(MEGLIO, ATT, {"tipo": "tutte", "valore": "", "escludi": [{"sede": "OSPEDALE A", "comune": "ASTI"}]})


def test_zona_di_e_descrizione_con_le_escluse(b):
    registra(b)
    p = pratica(b)
    assert "escludi" not in botmod.zona_di(p) and "senza" not in botmod.descr_zona(botmod.zona_di(p), botmod.attuale_di(p))
    p["escluse"] = [TORINO_A]
    z = botmod.zona_di(p)
    assert z["escludi"] == [TORINO_A] and z["tipo"] == "sede"
    assert botmod.descr_zona(z, botmod.attuale_di(p)).endswith(", senza Ospedale A")


def test_non_voglio_questa_sede_dall_offerta_e_rimetterla(b):
    registra(b)
    cerca_a_torino(b)
    b.controlla(pratica(b))
    [ex] = [x for x in tutti_pulsanti(b) if x.startswith("ex:")]
    assert len(ex.encode()) <= 64
    b.on_callback(cq(2, ex))  # un'altra chat: la ricetta non e' sua
    assert not pratica(b).get("escluse")
    b.on_callback(cq(1, ex))
    assert pratica(b)["escluse"] == [TORINO_A]
    assert "Non ti propongo più date in Ospedale A" in inviati(b)[-1]
    assert 1 not in b.offerte  # l'offerta aveva solo questa sede: non c'e' piu' nulla da prenotare
    # la stessa sede non torna piu' nelle offerte, anche se il bot non l'aveva ancora notificata
    p = pratica(b)
    p["notificati"] = {}
    b.salva(p, "notificati")
    b.out.clear()
    b.controlla(pratica(b))
    assert not any("C'e'" in t for t in inviati(b))
    # /escluse la elenca e la rimette
    b.out.clear()
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/escluse"))
    assert "Sedi escluse" in inviati(b)[-1] and "Ospedale A" in inviati(b)[-1]
    assert botmod.zona_di(pratica(b))["escludi"] == [TORINO_A]
    [es] = [x for x in tutti_pulsanti(b) if x.startswith("es:")]
    b.on_callback(cq(1, es))
    assert not pratica(b).get("escluse") and "Ho rimesso Ospedale A" in inviati(b)[-1]
    b.on_callback(cq(1, es))  # secondo tocco: gia' rimessa
    assert "non è più tra quelle escluse" in inviati(b)[-1]
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/escluse"))
    assert "Nessuna sede esclusa" in inviati(b)[-1]


def test_il_pulsante_funziona_anche_senza_l_offerta_in_memoria(b):
    registra(b)
    cerca_a_torino(b)
    b.controlla(pratica(b))
    [ex] = [x for x in tutti_pulsanti(b) if x.startswith("ex:")]
    b.offerte.clear()  # riavvio, o un'offerta piu' nuova: la sede si ritrova da quelle viste
    b.on_callback(cq(1, ex))
    assert pratica(b)["escluse"] == [TORINO_A]
    b.on_callback(cq(1, ex.rsplit(":", 1)[0] + ":zzzzzzzz"))  # una sede mai vista: niente
    assert len(pratica(b)["escluse"]) == 1 and "Non riconosco" in inviati(b)[-1]


def test_non_si_esclude_la_sede_in_cui_si_cerca(b):
    registra(b)  # zona predefinita: la sede della prenotazione
    p = pratica(b)
    assert b.escludi_sede(p, "OSPEDALE A", "TORINO") is False
    assert "proprio dove cerco" in inviati(b)[-1] and not pratica(b).get("escluse")
    p["zona"] = {"tipo": "sedi", "valore": [TORINO_A, {"sede": "OSPEDALE C", "comune": "MONCALIERI"}]}
    assert b.escludi_sede(p, "OSPEDALE A", "TORINO") is True  # ne resta una
    assert b.escludi_sede(p, "OSPEDALE C", "MONCALIERI") is False  # sarebbero tutte fuori
    assert [x["sede"] for x in pratica(b)["escluse"]] == ["OSPEDALE A"]


def test_le_escluse_non_restano_dentro_la_zona(b):
    registra(b)
    p = pratica(b)
    p["zona"] = {"tipo": "tutte", "valore": "", "escludi": [TORINO_A]}  # una copia vecchia dentro la zona
    assert "escludi" not in botmod.zona_di(p)
    p["escluse"] = [TORINO_A]
    assert botmod.zona_di(p)["escludi"] == [TORINO_A]
    del p["escluse"]
    assert "escludi" not in botmod.zona_di(p)  # rimettendo tutto, non resta nulla


def test_escludere_due_volte_la_stessa_sede_o_troppe(b):
    registra(b)
    cerca_a_torino(b)
    p = pratica(b)
    b.escludi_sede(p, "OSPEDALE A", "TORINO")
    b.escludi_sede(p, "OSPEDALE A", "TORINO")
    assert pratica(b)["escluse"] == [TORINO_A] and "già tra le sedi escluse" in inviati(b)[-1]
    for n in range(c.ESCLUSE_MAX):
        b.escludi_sede(p, f"SEDE {n}", "TORINO")
    assert len(pratica(b)["escluse"]) == c.ESCLUSE_MAX and "al massimo" in inviati(b)[-1]


def test_foglio_della_mini_app_aggiunge_e_toglie(app):
    pid = pratica(app.bot, 1)["id"]
    assert "Escludi una sede" in get(app, "/ui/ricette")[2].decode()
    foglio = get(app, f"/ui/r/{pid}/escluse")[2].decode()
    assert "Nessuna sede esclusa" in foglio and "Ospedale A" in foglio  # la sede della prenotazione e' tra le scelte
    valore = webapp.json_sedi([ATT.luogo.sede, c.comune(ATT.luogo)])
    stato, _, errore = post(app, f"/ui/r/{pid}/escluse", {"fai": "aggiungi", "sede": valore})
    assert stato == 409 and "proprio dove cerchi" in errore.decode() and "escluse" not in app.store.get(pid)
    cerca_a_torino(app.bot)  # ora la zona e' il comune: la sede si puo' escludere
    stato, _, foglio = post(app, f"/ui/r/{pid}/escluse", {"fai": "aggiungi", "sede": valore})
    foglio = foglio.decode()
    assert stato == 200 and app.store.get(pid)["escluse"] == [TORINO_A]
    assert post(app, f"/ui/r/{pid}/escluse", {"fai": "aggiungi", "sede": valore})[0] == 200  # doppio tocco
    assert "Rimetti" in foglio and "Nessuna sede esclusa" not in foglio
    assert "Sedi escluse (1)" in get(app, "/ui/ricette")[2].decode()
    assert botmod.zona_di(app.store.get(pid))["escludi"] == [TORINO_A]
    # una sede mai vista nei controlli non si puo' aggiungere
    stato, _, _ = post(app, f"/ui/r/{pid}/escluse", {"fai": "aggiungi", "sede": webapp.json_sedi(["POSTO INVENTATO", "X"])})
    assert stato == 400 and len(app.store.get(pid)["escluse"]) == 1
    for dati in ({"fai": "altro", "sede": valore}, {"fai": "togli", "sede": "non json"}, {"fai": "togli"}):
        assert post(app, f"/ui/r/{pid}/escluse", dati)[0] == 400
    stato, _, foglio = post(app, f"/ui/r/{pid}/escluse", {"fai": "togli", "sede": valore})
    assert stato == 200 and "escluse" not in app.store.get(pid) and "Nessuna sede esclusa" in foglio.decode()


def test_foglio_escluse_solo_per_le_proprie_ricette(app):
    pid = pratica(app.bot, 1)["id"]
    cerca_a_torino(app.bot)
    assert get(app, f"/ui/r/{pid}/escluse", chat=2)[0] == 404
    valore = webapp.json_sedi([ATT.luogo.sede, c.comune(ATT.luogo)])
    assert post(app, f"/ui/r/{pid}/escluse", {"fai": "aggiungi", "sede": valore}, chat=2)[0] == 404
    assert "escluse" not in app.store.get(pid)
