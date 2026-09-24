"""Test senza rete: parser su HTML sintetico (stessa struttura del portale, dati inventati),
cifratura dell'archivio e flusso del bot con Telegram e portale finti."""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot as botmod  # noqa: E402
import cup_http as c  # noqa: E402
from store import Store  # noqa: E402

CF, NRE = "RSSMRA80A01L219X", "010A00000000001"


def blocco(quando, sede, amb, zona=False, seleziona_id=None, cosa="VISITA DI ESEMPIO - 11.11"):
    zone = ('<div><span id="z1">AZIENDA ESEMPIO [MACRO-ZONA]</span><span id="z2"> - </span>'
            '<span id="z3">AZIENDA ESEMPIO [ZONA]</span> </div>') if zona else "<div> </div>"
    btn = (f'<div class=" btn btn-primary iconButton icon-ok wideButtons mb-5" id="{seleziona_id}" onmouseover="">'
           '<span><span><button type="button"><span>Seleziona</span></button></span></span></div>') if seleziona_id else ""
    return (f'<div class="captionAppointment-what"><span>Cosa:</span> </div>'
            f'<div class="captionAppointment-desc"><span id="d">{cosa}</span><span> (PRENOTABILE)</span> <br /> </div>'
            f'<span>Quando:</span><span class="fw-b">{quando}</span><span>alle ore</span><span class="fw-b">09:00</span>'
            f'<span>Dove: </span><br /><span>Mappa</span> </div> <div style="flex-grow:1;"> {zone}'
            f'<div><span id="s">{sede}</span> </div> <div><span id="a">{amb}</span> </div> '
            f'<div class="unita-address" data-address="Via Roma, 1"><span>Via Roma, 1</span><span> - TORINO (TO)</span> </div>'
            f'</div>{btn}')


def test_parser_luogo_data_cosa():
    html_ = "<span>Stato:</span> PRENOTATO " + blocco("Lunedì 1 Marzo 2027", "OSPEDALE A", "AMB 1", zona=True)
    assert c._date(c._text(html_)) == datetime(2027, 3, 1, 9, 0)
    assert c._luogo(html_) == c.Luogo("OSPEDALE A", "AMB 1", "Via Roma, 1 - TORINO (TO)")
    assert c._cosa(html_) == "VISITA DI ESEMPIO - 11.11"


def test_mese_sconosciuto_non_e_una_data():
    assert c._date("Lunedì 1 Brumaio 2027 alle ore 09:00") is None


def test_form_fields_come_il_browser():
    page = ('<form id="F" action="x"><input type="hidden" name="F" value="F" /><input type="text" name="tel" value="123" />'
            '<input type="checkbox" name="no" /><input type="checkbox" name="si" checked="checked" value="on" />'
            '<select name="p"><option value="A">A</option><option value="D" selected="true">D</option></select></form>')
    assert c._form_fields(page, "F") == {"F": "F", "tel": "123", "si": "on", "p": "D"}


def test_riepilogo_deve_combaciare():
    slot = c.Slot(datetime(2027, 3, 1, 9, 0), c.Luogo("OSPEDALE A", "AMB 1", ""), "id")
    testo = "Prestazioni selezionate: 1 VISITA DI ESEMPIO - 11.11 Quando Lunedì 1 Marzo 2027 alle ore 09:00 OSPEDALE A - AMB 1 - - Via Roma"
    m = c.DATE_RE.search(testo)
    c._verifica_riepilogo(testo, c._date(testo), testo[m.end():], slot, "VISITA DI ESEMPIO - 11.11")
    with pytest.raises(c.CupError):  # altra sede, stessa ora
        altro = c.Slot(slot.quando, c.Luogo("OSPEDALE B", "AMB 1", ""), "id")
        c._verifica_riepilogo(testo, c._date(testo), testo[m.end():], altro, "VISITA DI ESEMPIO - 11.11")
    with pytest.raises(c.CupError):  # altra prestazione
        c._verifica_riepilogo(testo, c._date(testo), testo[m.end():], slot, "ALTRA VISITA - 22.22")
    with pytest.raises(c.CupError):  # altra data
        c._verifica_riepilogo(testo, datetime(2027, 3, 2, 9, 0), testo[m.end():], slot, "")


def test_store_cifra_i_dati(tmp_path):
    key = Fernet.generate_key().decode()
    s = Store(tmp_path / "db.sqlite", key)
    u = s.new(42)
    u.update(cf=CF, nre=NRE)
    s.save(u)
    raw = (tmp_path / "db.sqlite").read_bytes()
    assert CF.encode() not in raw and NRE.encode() not in raw
    assert s.get(42)["cf"] == CF
    s.delete(42)
    assert s.get(42) is None


# --- bot con Telegram e portale finti ---------------------------------------------------
ATT = c.Prenotazione(datetime.now() + timedelta(days=200), c.Luogo("OSPEDALE A", "AMB 1", "Via Roma, 1"), "VISITA - 11.11")
MEGLIO = c.Slot(datetime.now() + timedelta(days=30), c.Luogo("OSPEDALE A", "AMB 2", "Via Roma, 1"), None)
ALTROVE = c.Slot(datetime.now() + timedelta(days=20), c.Luogo("OSPEDALE B", "AMB 9", "Via Po, 2"), "id")


@pytest.fixture
def b(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite", Fernet.generate_key().decode())
    bot = botmod.Bot(store, "x", admin="999", distanza=0)
    bot.out = []

    def tg(method, **d):
        bot.out.append((method, d))
        return {"ok": True}
    bot.tg = tg
    bot.chiamate = []
    monkeypatch.setattr(c, "cerca", lambda cf, nre: ATT)
    monkeypatch.setattr(c, "check", lambda cf, nre, stessa: {
        "attuale": ATT, "slots": [ALTROVE, MEGLIO], "sessione": "S",
        "migliori": [x for x in (ALTROVE, MEGLIO) if not stessa or x.luogo.sede == ATT.luogo.sede]})

    def prenota(cf, nre, slot, sessione=None, stessa_sede=True, dry_run=True):
        bot.chiamate.append((cf, slot.key(), sessione, dry_run))
        return "Prenotazione spostata."
    monkeypatch.setattr(c, "prenota", prenota)
    return bot


def msg(chat, text, mid=1, tipo="private"):
    return {"chat": {"id": chat, "type": tipo}, "from": {"id": chat}, "message_id": mid, "text": text}


def cq(chat, data):
    return {"id": "q", "from": {"id": chat}, "message": {"message_id": 5, "chat": {"id": chat}}, "data": data}


def inviati(b):
    return [d["text"] for m, d in b.out if m == "sendMessage"]


def registra(b, chat=1, stessa="1", nre=NRE):
    b.on_message(msg(chat, "/start"))
    b.on_callback(cq(chat, "consenso:1"))
    b.on_message(msg(chat, CF.lower(), mid=10))
    b.on_message(msg(chat, nre, mid=11))
    b.on_callback(cq(chat, "sede:" + stessa))


def test_registrazione_completa_e_cancella_i_messaggi(b):
    registra(b)
    u = b.store.get(1)
    assert u["stato"] == "attivo" and u["cf"] == CF and u["nre"] == NRE and u["stessa_sede"] is True
    cancellati = {d["message_id"] for m, d in b.out if m == "deleteMessage"}
    assert {10, 11} <= cancellati
    assert any("OSPEDALE A" in t and "📅" in t for t in inviati(b))  # data, ora e luogo mostrati


def test_cf_non_valido(b):
    b.on_message(msg(1, "/start"))
    b.on_callback(cq(1, "consenso:1"))
    b.on_message(msg(1, "ciao"))
    assert b.store.get(1)["stato"] == "cf"


def test_rifiuto_consenso_non_conserva_nulla(b):
    b.on_message(msg(1, "/start"))
    b.on_callback(cq(1, "consenso:0"))
    assert b.store.get(1) is None


def test_gruppi_ignorati(b):
    b.on_message(msg(-5, "/start", tipo="group"))
    assert b.store.get(-5) is None and not inviati(b)


def test_offerta_e_prenotazione_nella_stessa_sessione(b):
    registra(b)
    u = b.store.get(1)
    b.controlla(u)
    offerta = [d for m, d in b.out if m == "sendMessage" and d.get("reply_markup")][-1]
    pulsanti = offerta["reply_markup"]["inline_keyboard"]
    assert len(pulsanti) == 2  # solo la data nella stessa sede + Ignora
    assert "OSPEDALE A" in offerta["text"]
    b.on_callback(cq(2, pulsanti[0][0]["callback_data"]))  # utente estraneo: niente
    assert not b.chiamate
    b.on_callback(cq(1, pulsanti[0][0]["callback_data"]))
    assert b.chiamate == [(CF, MEGLIO.key(), "S", False)]
    b.on_callback(cq(1, pulsanti[0][0]["callback_data"]))  # doppio tocco
    assert len(b.chiamate) == 1
    assert any(t.startswith("✅ Prenotazione spostata") and "OSPEDALE A" in t for t in inviati(b))


def test_non_riofferta_la_stessa_data(b):
    registra(b)
    b.controlla(b.store.get(1))
    b.offerte.clear()
    n = len(inviati(b))
    b.controlla(b.store.get(1))
    assert len(inviati(b)) == n


def test_qualsiasi_sede(b):
    registra(b, stessa="0")
    b.controlla(b.store.get(1))
    offerta = [d for m, d in b.out if m == "sendMessage" and d.get("reply_markup")][-1]
    assert len(offerta["reply_markup"]["inline_keyboard"]) == 3


def test_offerta_scaduta(b):
    registra(b)
    b.controlla(b.store.get(1))
    token = b.offerte[1]["token"]
    b.offerte[1]["ts"] -= botmod.TTL_OFFERTA + 1
    b.on_callback(cq(1, f"p:{token}:0"))
    assert not b.chiamate and b.store.get(1)["notificati"] == []


def test_prenotazione_non_piu_attiva_mette_in_pausa(b, monkeypatch):
    registra(b)

    def gone(*a):
        raise c.NonAttiva("La prenotazione risulta in stato DISDETTO")
    monkeypatch.setattr(c, "check", gone)
    b.controlla(b.store.get(1))
    assert b.store.get(1)["stato"] == "pausa"


def test_data_passata_cancella_i_dati(b, monkeypatch):
    registra(b)
    passata = c.Prenotazione(datetime.now() - timedelta(days=1), ATT.luogo, ATT.cosa)
    monkeypatch.setattr(c, "check", lambda *a: {"attuale": passata, "slots": [], "migliori": [], "sessione": None})
    b.controlla(b.store.get(1))
    assert b.store.get(1) is None


def test_cancella(b):
    registra(b)
    b.on_message(msg(1, "/cancella"))
    b.on_callback(cq(1, "del:1"))
    assert b.store.get(1) is None


def test_errore_imprevisto_avvisa_admin_senza_dati(b, monkeypatch):
    registra(b)

    def boom(*a):
        raise KeyError("x")
    monkeypatch.setattr(c, "check", boom)
    b.controlla(b.store.get(1))
    admin = [d["text"] for m, d in b.out if m == "sendMessage" and d["chat_id"] == "999"]
    assert admin and CF not in admin[0] and NRE not in admin[0]


def test_scheduler_salta_chi_ha_un_offerta_aperta(b):
    registra(b)
    registra(b, chat=2, nre="010A00000000002")
    for chat in (1, 2):
        u = b.store.get(chat)
        u["prossimo"] = 0
        b.store.save(u)
    b.offerte[1] = {"token": "t", "ts": time.time(), "sessione": "S", "slots": []}
    assert b.controllo_pianificato()
    assert b.store.get(1)["prossimo"] == 0 and b.store.get(2)["prossimo"] > time.time()


def test_limite_utenti(b):
    b.max_utenti = 1
    registra(b, chat=1)
    b.on_message(msg(2, "/start"))
    b.on_callback(cq(2, "consenso:1"))
    assert b.store.get(2) is None


def test_nessun_dato_prima_del_consenso(b):
    b.on_message(msg(1, "/start"))
    assert b.store.get(1) is None and b.store.count() == 0


def test_stessa_ricetta_un_solo_utente(b):
    registra(b, chat=1)
    registra(b, chat=2)
    assert b.store.get(1)["stato"] == "attivo" and b.store.get(2)["stato"] == "cf"
    assert any("gia' seguita" in t for t in inviati(b))


def test_ricerche_fallite_limitate(b, monkeypatch):
    def nessuna(cf, nre):
        raise c.NonTrovata("Non esistono richieste")
    monkeypatch.setattr(c, "cerca", nessuna)
    b.on_message(msg(1, "/start"))
    b.on_callback(cq(1, "consenso:1"))
    for i in range(botmod.MAX_RICERCHE_FALLITE + 2):
        b.ultimo_msg.clear()
        b.on_message(msg(1, CF, mid=20 + 2 * i))
        b.on_message(msg(1, NRE, mid=21 + 2 * i))
    assert any("Troppe ricerche" in t for t in inviati(b))


def test_controlla_non_a_raffica(b):
    registra(b)
    b.controlla(b.store.get(1))
    b.offerte.clear()
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/controlla"))
    assert "il prossimo /controlla" in inviati(b)[-1]


def test_token_oscurato_nei_log(b):
    b.token = "123:SEGRETO"
    assert "SEGRETO" not in b.redact(Exception("url: /bot123:SEGRETO/getUpdates"))


def test_bot_bloccato_cancella_i_dati(b, monkeypatch):
    registra(b)

    class R:
        def json(self):
            return {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}
    monkeypatch.setattr(botmod.requests, "post", lambda *a, **k: R())
    botmod.Bot.tg(b, "sendMessage", chat_id=1, text="ciao")
    assert b.store.get(1) is None


def test_luogo_illeggibile_non_si_offre_ne_si_conferma(b, monkeypatch):
    registra(b, stessa="0")
    vuoto = c.Slot(datetime.now() + timedelta(days=3), c.Luogo("", "", ""), None)
    monkeypatch.setattr(c, "check", lambda *a: {"attuale": ATT, "slots": [vuoto], "migliori": [vuoto], "sessione": "S"})
    n = len(inviati(b))
    b.controlla(b.store.get(1))
    assert len(inviati(b)) == n  # nessuna offerta
    assert not c.ammesso(vuoto, ATT, False)
    with pytest.raises(c.CupError):
        c._verifica_riepilogo("x", vuoto.quando, "", vuoto, "VISITA - 11.11")


def test_cf_scritto_fuori_registrazione_viene_cancellato(b):
    registra(b)
    b.out.clear()
    b.ultimo_msg.clear()
    b.on_message(msg(1, CF, mid=77))
    assert ("deleteMessage", {"chat_id": 1, "message_id": 77}) in b.out


def attiva_auto(b, chat=1, giorni=1):
    b.on_callback(cq(chat, f"auto:{giorni}"))
    assert b.store.get(chat)["auto"] == {"giorni": giorni}


def test_auto_prenota_senza_tocco_nella_stessa_sessione(b):
    registra(b)
    attiva_auto(b)
    b.controlla(b.store.get(1))
    assert b.chiamate == [(CF, MEGLIO.key(), "S", False)]
    assert 1 not in b.offerte  # nessun pulsante: ha gia' prenotato
    assert any("conferma automatica" in t and "📅" in t and "OSPEDALE A" in t for t in inviati(b))


def test_auto_rispetta_anticipo_minimo(b, monkeypatch):
    registra(b)
    attiva_auto(b, giorni=7)
    domani = c.Slot(datetime.now() + timedelta(days=1), c.Luogo("OSPEDALE A", "AMB 2", "Via Roma, 1"), None)
    monkeypatch.setattr(c, "check", lambda *a: {"attuale": ATT, "slots": [domani], "migliori": [domani], "sessione": "S"})
    b.controlla(b.store.get(1))
    assert not b.chiamate and 1 in b.offerte  # troppo presto: solo offerta manuale


def test_auto_rispetta_la_sede(b):
    registra(b)  # solo stessa sede: ALTROVE (piu' vicina) non va presa
    attiva_auto(b)
    b.controlla(b.store.get(1))
    assert b.chiamate[0][1] == MEGLIO.key()


def test_auto_un_solo_tentativo_per_data(b, monkeypatch):
    registra(b)
    attiva_auto(b)
    monkeypatch.setattr(c, "prenota", lambda *a, **k: (_ for _ in ()).throw(c.CupError("Slot non piu' disponibile")))
    b.controlla(b.store.get(1))
    n = len(inviati(b))
    b.controlla(b.store.get(1))
    assert len(inviati(b)) == n


def test_auto_si_disattiva_dopo_esito_incerto(b, monkeypatch):
    registra(b)
    attiva_auto(b)
    monkeypatch.setattr(c, "prenota", lambda *a, **k: (_ for _ in ()).throw(
        c.CupError("Conferma inviata, esito incerto: la prenotazione risulta non verificabile.")))
    b.controlla(b.store.get(1))
    assert b.store.get(1)["auto"] is None
    assert any(t.startswith("🚨") for t in inviati(b))


def test_auto_disattivata_di_default_e_disattivabile(b):
    registra(b)
    assert not b.store.get(1).get("auto")
    attiva_auto(b, giorni=3)
    b.on_callback(cq(1, "auto:0"))
    assert b.store.get(1)["auto"] is None
    b.on_callback(cq(1, "auto:99"))  # valore non previsto: ignorato
    assert b.store.get(1)["auto"] is None


def test_intervallo_breve_solo_per_admin(b):
    b.admin, b.admin_intervallo = "1", 5
    registra(b, chat=1)
    registra(b, chat=2, nre="010A00000000002")
    assert b.intervallo_di(1) == 5 and b.intervallo_di(2) == 45
    for chat in (1, 2):
        u = b.store.get(chat)
        u["prossimo"] = 0
        b.store.save(u)
    b.controllo_pianificato()
    b.offerte.clear()
    b.controllo_pianificato()
    prossimi = {chat: b.store.get(chat)["prossimo"] - time.time() for chat in (1, 2)}
    assert prossimi[1] < 6 * 60 and prossimi[2] > 40 * 60


def test_intervallo_utenti_mai_sotto_il_minimo(tmp_path):
    s = Store(tmp_path / "db.sqlite", Fernet.generate_key().decode())
    assert botmod.Bot(s, "x", intervallo=5).intervallo == botmod.MIN_INTERVALLO
    assert botmod.Bot(s, "x", admin="1", admin_intervallo=1).admin_intervallo == botmod.MIN_INTERVALLO_ADMIN


def test_menu_con_tutti_i_comandi(b):
    b.admin = "999"
    b.imposta_menu()
    menu = {d["scope"]["type"]: [c["command"] for c in d["commands"]] for m, d in b.out if m == "setMyCommands"}
    for scope in ("default", "all_private_chats", "chat"):
        assert {"auto", "sede", "dati", "cancella", "help"} <= set(menu[scope])
    assert "admin" in menu["chat"] and "admin" not in menu["default"]
    comandi_aiuto = {w[1:].strip(",.") for w in botmod.AIUTO.split() if w.startswith("/")}
    assert {c for c, _ in botmod.COMANDI if c != "help"} <= comandi_aiuto


def test_pulizia(tmp_path):
    s = Store(tmp_path / "db.sqlite", Fernet.generate_key().decode())
    vecchia = s.new(1)
    vecchia["creato"] = time.time() - 2 * 86400
    s.save(vecchia)
    s.new(2)
    p = s.new(3)
    p.update(stato="pausa", pausa_da=time.time() - 31 * 86400)
    s.save(p)
    assert sorted(s.pulizia(time.time())) == [1, 3] and s.get(2)
