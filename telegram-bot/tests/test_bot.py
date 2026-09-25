"""Test senza rete: parser su HTML sintetico (stessa struttura del portale, dati inventati),
archivio cifrato e flusso del bot con Telegram e portale finti."""
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot as botmod  # noqa: E402
import cup_http as c  # noqa: E402
from store import Store  # noqa: E402

CF, NRE = "RSSMRA80A01L219X", "010A00000000001"
CF2, NRE2 = "VRDGPP50A01A859Q", "010A00000000009"
CF3, NRE3 = "GLLMRC75C12D969K", "010A00000000003"  # ricetta mai prenotata


# --- parser ------------------------------------------------------------------------------
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


def test_zone_e_ammesso():
    att = c.Prenotazione(datetime(2027, 1, 1), c.Luogo("OSP ASTI", "ESAME", "Via X - ASTI (AT)"), "ESAME")
    alba = c.Slot(datetime(2026, 12, 1), c.Luogo("OSP ALBA", "ESAME", "Via Y - ALBA (CN)"), "id")
    bra = c.Slot(datetime(2026, 12, 1), c.Luogo("OSP BRA", "ESAME", "Via Z - BRA (CN)"), "id")
    senza = c.Slot(datetime(2026, 12, 1), c.Luogo("OSP S", "ESAME", "Via W - ()"), "id")
    z = {"tipo": "comune", "valore": "ALBA"}
    assert c.ammesso(alba, att, z) and not c.ammesso(bra, att, z) and not c.ammesso(senza, att, z)
    assert c.ammesso(bra, att, {"tipo": "provincia", "valore": "CN"})
    assert not c.ammesso(alba, att, "sede") and c.ammesso(alba, att, "tutte")
    assert c.zona_norm(True) == {"tipo": "sede", "valore": ""} and c.zona_norm(False)["tipo"] == "tutte"
    assert c.estensioni(z) == c.ESTENDI_MAX and c.estensioni("sede") == 0
    assert c.provincia(alba.luogo) == "CN" and c.comune(bra.luogo) == "BRA"


def test_slot_non_selezionabile():
    s = c.CupSession(CF, NRE)
    with pytest.raises(c.CupError):
        s.riepilogo(c.Slot(datetime(2027, 1, 1), c.Luogo("X", "Y", ""), None, proposta=False))


# --- archivio ----------------------------------------------------------------------------
def test_store_cifra_i_dati(tmp_path):
    s = Store(tmp_path / "db.sqlite", Fernet.generate_key().decode())
    p = s.new(42, cf=CF, nre=NRE)
    raw = (tmp_path / "db.sqlite").read_bytes()
    assert CF.encode() not in raw and NRE.encode() not in raw
    assert s.get(p["id"])["cf"] == CF and s.della_chat(42)[0]["id"] == p["id"]
    s.delete(p["id"])
    assert s.get(p["id"]) is None


def test_migrazione_dalla_versione_con_una_ricetta_per_chat(tmp_path):
    key = Fernet.generate_key().decode()
    db = sqlite3.connect(tmp_path / "db.sqlite")
    db.execute("CREATE TABLE utenti (chat_id INTEGER PRIMARY KEY, stato TEXT, prossimo REAL, errori INTEGER, "
               "creato REAL, dati BLOB, coppia TEXT UNIQUE)")
    dati = Fernet(key).encrypt(json.dumps({"cf": CF, "nre": NRE, "stessa_sede": True}).encode())
    db.execute("INSERT INTO utenti VALUES (7, 'attivo', 0, 0, 1, ?, 'h')", (dati,))
    db.commit()
    db.close()
    s = Store(tmp_path / "db.sqlite", key)
    [p] = s.della_chat(7)
    assert p["cf"] == CF and p["stato"] == "attivo" and botmod.zona_di(p)["tipo"] == "sede"
    assert not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='utenti'").fetchone()


def test_pulizia(tmp_path):
    s = Store(tmp_path / "db.sqlite", Fernet.generate_key().decode())
    vecchia = s.new(1)
    vecchia["creato"] = time.time() - 2 * 86400
    s.save(vecchia)
    s.new(2)
    p = s.new(3)
    p.update(stato="pausa", pausa_da=time.time() - 31 * 86400)
    s.save(p)
    assert sorted(ch for _, ch in s.pulizia(time.time())) == [1, 3] and s.della_chat(2)


# --- bot con Telegram e portale finti ----------------------------------------------------
ATT = c.Prenotazione(datetime.now() + timedelta(days=200), c.Luogo("OSPEDALE A", "AMB 1", "Via Roma, 1 - TORINO (TO)"),
                     "VISITA - 11.11")
MEGLIO = c.Slot(datetime.now() + timedelta(days=30), c.Luogo("OSPEDALE A", "AMB 2", "Via Roma, 1 - TORINO (TO)"), None,
                proposta=True)
VICINO = c.Slot(datetime.now() + timedelta(days=25), c.Luogo("OSPEDALE C", "AMB 3", "Via Po, 3 - MONCALIERI (TO)"), "id2")
ALTROVE = c.Slot(datetime.now() + timedelta(days=20), c.Luogo("OSPEDALE B", "AMB 9", "Via Roma, 2 - CUNEO (CN)"), "id")
# ricetta del familiare: prenotazione ad Asti, si cercano date ad Alba
ATT2 = c.Prenotazione(datetime.now() + timedelta(days=60), c.Luogo("CLINICA ASTI", "ESAME", "Via X - ASTI (AT)"), "ESAME - 99.99")
ASTI = c.Slot(datetime.now() + timedelta(days=10), c.Luogo("CLINICA ASTI", "ESAME", "Via X - ASTI (AT)"), "b1")
ALBA = c.Slot(datetime.now() + timedelta(days=40), c.Luogo("OSP ALBA", "ESAME 2", "Via Roma, 9 - ALBA (CN)"), "t1")
# ricetta mai prenotata: il CUP propone una sede a Torino e una a Cuneo
COSA3 = "ECOGRAFIA ADDOME COMPLETO"
NUOVA_TO = c.Slot(datetime.now() + timedelta(days=15), c.Luogo("POLIAMBULATORIO NORD", "ECO 1", "Via Po, 5 - TORINO (TO)"), "n1")
NUOVA_CN = c.Slot(datetime.now() + timedelta(days=9), c.Luogo("OSPEDALE B", "ECO", "Via Roma, 2 - CUNEO (CN)"), "n2")


@pytest.fixture
def b(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite", Fernet.generate_key().decode())
    bot = botmod.Bot(store, "x", admin="999", distanza=0)
    bot.out = []

    def tg(method, **d):
        bot.out.append((method, d))
        return {"ok": True, "result": {"message_id": len(bot.out) + 1000}}
    bot.tg = tg
    bot.chiamate = []
    bot.zone_viste = []

    def vietato(*a, **k):
        raise AssertionError("richiesta di rete in un test")
    monkeypatch.setattr(requests.Session, "request", vietato)  # mai il portale vero dai test
    bot.nuove = {}  # ricetta mai prenotata (CF3) -> prenotazione, dopo la prima prenotazione
    bot.prenotata_a_mano = False

    def cerca(cf, nre):
        if cf == CF3:
            if cf in bot.nuove:
                return bot.nuove[cf]
            raise c.NonTrovata("Non esistono prenotazioni")
        return ATT2 if cf == CF2 else ATT
    monkeypatch.setattr(c, "cerca", cerca)

    def nuova(cf, nre):
        if cf == CF3 and cf not in bot.nuove:
            return COSA3
        if cf == CF3:
            raise c.GiaPrenotata("Numero ricetta elettronica già presente")
        raise c.NonTrovata("Numero ricetta elettronica non valido")
    monkeypatch.setattr(c, "nuova", nuova)

    def check_nuova(cf, nre, zona="tutte"):
        if cf in bot.nuove:
            raise c.GiaPrenotata("Numero ricetta elettronica già presente")
        bot.zone_viste.append(zona)
        slots = sorted([NUOVA_TO, NUOVA_CN], key=lambda x: x.quando)  # come il client vero
        return {"attuale": None, "cosa": COSA3, "slots": slots, "sessione": f"S-{cf}",
                "migliori": [x for x in slots if c.ammesso(x, None, zona)]}
    monkeypatch.setattr(c, "check_nuova", check_nuova)

    def check(cf, nre, zona):
        bot.zone_viste.append(zona)
        if cf in bot.nuove:  # la ricetta prima mai prenotata, dopo la prima prenotazione
            att, slots = bot.nuove[cf], sorted([NUOVA_TO, NUOVA_CN], key=lambda x: x.quando)
        else:
            att, slots = (ATT2, [ASTI, ALBA]) if cf == CF2 else (ATT, [ALTROVE, VICINO, MEGLIO])
        return {"attuale": att, "slots": slots, "sessione": f"S-{cf}",
                "migliori": [x for x in slots if x.quando < att.quando and c.ammesso(x, att, zona)]}
    monkeypatch.setattr(c, "check", check)

    bot.libere = []

    def prenota(cf, nre, slot, sessione=None, zona="sede", dry_run=True, libera=False, nuova=False):
        bot.chiamate.append((cf, slot.key(), sessione, dry_run))
        bot.libere.append(libera)
        assert nuova == (cf == CF3 and cf not in bot.nuove)  # il bot dice sempre al client se e' la prima
        if nuova and bot.prenotata_a_mano:  # qualcuno l'ha prenotata sul portale un attimo prima
            bot.nuove[cf] = c.Prenotazione(NUOVA_TO.quando, NUOVA_TO.luogo, COSA3)
            raise c.GiaPrenotata(f"Nel frattempo la ricetta risulta prenotata al {NUOVA_TO.quando:%d/%m/%Y %H:%M}")
        if nuova and not dry_run:
            bot.nuove[cf] = c.Prenotazione(slot.quando, slot.luogo, COSA3)
            return "Prenotazione fatta."
        return "Prenotazione spostata."
    monkeypatch.setattr(c, "prenota", prenota)
    return bot


def msg(chat, text, mid=1, tipo="private"):
    return {"chat": {"id": chat, "type": tipo}, "from": {"id": chat}, "message_id": mid, "text": text}


def cq(chat, data):
    return {"id": "q", "from": {"id": chat}, "message": {"message_id": 5, "chat": {"id": chat}}, "data": data}


def inviati(b):
    """Messaggi mandati, escluso il pannello (che ha i suoi test)."""
    return [d["text"] for m, d in b.out if m == "sendMessage" and not d["text"].startswith("📋")]


def pulsanti(b):
    """callback_data dei pulsanti dell'ultimo messaggio (non pannello) che ne aveva."""
    ultimo = [d for m, d in b.out if m == "sendMessage" and d.get("reply_markup") and not d["text"].startswith("📋")][-1]
    return [bt["callback_data"] for riga in ultimo["reply_markup"]["inline_keyboard"] for bt in riga]


def pratica(b, chat=1, n=0):
    return b.store.della_chat(chat)[n]


def scegli_sede(b, chat, tipo):
    [cb] = [x for x in pulsanti(b) if x.startswith("sede:") and x.endswith(":" + tipo)]
    b.on_callback(cq(chat, cb))


def registra(b, chat=1, zona="sede", nre=NRE, cf=CF):
    b.ultimo_msg.clear()
    b.on_message(msg(chat, "/start"))
    b.on_callback(cq(chat, "consenso:1"))
    b.on_message(msg(chat, cf.lower(), mid=10))
    b.on_message(msg(chat, nre, mid=11))
    if any(x.startswith("sede:") for x in pulsanti(b)):
        scegli_sede(b, chat, zona)


def aggiungi_familiare(b, chat=1, nome="Familiare", comune="Alba"):
    b.ultimo_msg.clear()
    b.on_message(msg(chat, "/aggiungi"))
    b.on_message(msg(chat, CF2, mid=20))
    b.on_message(msg(chat, NRE2, mid=21))
    b.on_message(msg(chat, nome, mid=22))
    scegli_sede(b, chat, "altro")
    b.on_message(msg(chat, comune, mid=23))


def test_registrazione_completa_e_cancella_i_messaggi(b):
    registra(b)
    p = pratica(b)
    assert p["stato"] == "attivo" and p["cf"] == CF and p["nre"] == NRE and p["zona"] == {"tipo": "sede", "valore": "OSPEDALE A"}
    assert {10, 11} <= {d["message_id"] for m, d in b.out if m == "deleteMessage"}
    assert any("OSPEDALE A" in t and "📅" in t for t in inviati(b))  # data, ora e luogo mostrati


def test_nessun_dato_prima_del_consenso(b):
    b.on_message(msg(1, "/start"))
    assert b.store.count() == 0
    b.on_callback(cq(1, "consenso:0"))
    assert b.store.count() == 0


def test_cf_non_valido(b):
    b.on_message(msg(1, "/start"))
    b.on_callback(cq(1, "consenso:1"))
    b.on_message(msg(1, "ciao"))
    assert pratica(b)["stato"] == "cf"


def test_gruppi_ignorati(b):
    b.on_message(msg(-5, "/start", tipo="group"))
    assert b.store.count() == 0 and not inviati(b)


def test_offerta_e_prenotazione_nella_stessa_sessione(b):
    registra(b)
    b.controlla(pratica(b))
    cb = pulsanti(b)
    assert len(cb) == 2  # solo la data nella stessa sede + Ignora
    b.on_callback(cq(2, cb[0]))  # utente estraneo: la pratica non e' sua
    assert not b.chiamate
    b.on_callback(cq(1, cb[0]))
    assert b.chiamate == [(CF, MEGLIO.key(), f"S-{CF}", False)]
    b.on_callback(cq(1, cb[0]))  # doppio tocco
    assert len(b.chiamate) == 1
    assert any(t.startswith("✅ Prenotazione spostata") and "OSPEDALE A" in t for t in inviati(b))


def test_familiare_con_nome_e_comune(b):
    registra(b)
    aggiungi_familiare(b)
    io, papa = b.store.della_chat(1)
    assert papa["nome"] == "Familiare" and papa["stato"] == "attivo" and papa["zona"] == {"tipo": "comune", "valore": "ALBA"}
    assert {20, 21} <= {d["message_id"] for m, d in b.out if m == "deleteMessage"}
    b.controlla(papa)
    assert b.zone_viste[-1] == {"tipo": "comune", "valore": "ALBA"}
    offerta = [d for m, d in b.out if m == "sendMessage" and d.get("reply_markup") and not d["text"].startswith("📋")][-1]
    assert offerta["text"].startswith("[Familiare]") and "OSP ALBA" in offerta["text"]
    cb = pulsanti(b)
    assert len(cb) == 2  # Alba si', Asti (piu' vicina ma fuori comune) no
    b.on_callback(cq(1, cb[0]))
    assert b.chiamate[-1][:2] == (CF2, ALBA.key())
    assert pratica(b, 1, 0)["attuale"]["sede"] == "OSPEDALE A"  # la mia prenotazione non cambia


def test_pulsanti_di_una_pratica_valgono_solo_per_lei(b):
    registra(b)
    aggiungi_familiare(b)
    io, papa = b.store.della_chat(1)
    b.controlla(papa)
    token_papa = b.offerte[papa["id"]]["token"]
    b.on_callback(cq(1, f"p:{io['id']}:{token_papa}:0"))  # token di papa' sulla pratica sbagliata
    assert not b.chiamate


def test_scelta_della_pratica_per_i_comandi(b):
    registra(b)
    aggiungi_familiare(b)
    io, papa = b.store.della_chat(1)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/pausa"))
    assert f"sc:pausa:{papa['id']}:{botmod.versione(papa)}" in pulsanti(b)
    b.on_callback(cq(1, f"sc:pausa:{papa['id']}:{botmod.versione(papa)}"))
    assert b.store.get(papa["id"])["stato"] == "pausa" and b.store.get(io["id"])["stato"] == "attivo"
    b.on_callback(cq(2, f"sc:riprendi:{papa['id']}:{botmod.versione(papa)}"))  # altra chat: niente
    assert b.store.get(papa["id"])["stato"] == "pausa"


def test_cancella_una_pratica_o_tutte(b):
    registra(b)
    aggiungi_familiare(b)
    io, papa = b.store.della_chat(1)
    b.on_callback(cq(1, f"del:{papa['id']}:{botmod.versione(papa)}:1"))
    assert [p["id"] for p in b.store.della_chat(1)] == [io["id"]]
    aggiungi_familiare(b)
    b.on_callback(cq(1, "del:tutte:1"))
    assert not b.store.della_chat(1)


def test_limite_pratiche_per_chat(b):
    b.max_pratiche = 1
    registra(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/aggiungi"))
    assert len(b.store.della_chat(1)) == 1 and "al massimo 1" in inviati(b)[-1]


def test_limite_utenti(b):
    b.max_utenti = 1
    registra(b, chat=1)
    b.on_message(msg(2, "/start"))
    b.on_callback(cq(2, "consenso:1"))
    assert not b.store.della_chat(2)


def test_stessa_ricetta_un_solo_utente(b):
    registra(b, chat=1)
    registra(b, chat=2)
    assert pratica(b, 1)["stato"] == "attivo" and pratica(b, 2)["stato"] == "cf"
    assert any("gia' seguita" in t for t in inviati(b))


def test_ricerche_fallite_limitate(b, monkeypatch):
    def nessuna(cf, nre):
        raise c.NonTrovata("Non esistono richieste")
    monkeypatch.setattr(c, "cerca", nessuna)
    b.on_message(msg(1, "/start"))
    b.on_callback(cq(1, "consenso:1"))
    for i in range(botmod.MAX_RICERCHE_FALLITE + 2):
        b.on_message(msg(1, CF, mid=20 + 2 * i))
        b.on_message(msg(1, NRE, mid=21 + 2 * i))
    assert any("Troppe ricerche" in t for t in inviati(b))


def test_non_riofferta_la_stessa_data(b):
    registra(b)
    b.controlla(pratica(b))
    b.offerte.clear()
    n = len(inviati(b))
    b.controlla(pratica(b))
    assert len(inviati(b)) == n


def test_offerta_persa_col_riavvio_viene_riproposta(b):
    registra(b)
    b.controlla(pratica(b))
    b.offerte.clear()
    p = pratica(b)
    p["notificati"] = {k: time.time() - botmod.TTL_OFFERTA - 1 for k in p["notificati"]}
    b.store.save(p)
    b.controlla(pratica(b))
    assert pratica(b)["id"] in b.offerte


def test_ignora_non_ripropone(b):
    registra(b)
    b.controlla(pratica(b))
    p = pratica(b)
    b.on_callback(cq(1, f"x:{p['id']}:{b.offerte[p['id']]['token']}"))
    p = pratica(b)
    p["notificati"] = {}
    b.store.save(p)
    b.controlla(pratica(b))
    assert p["id"] not in b.offerte


def test_qualsiasi_sede(b):
    registra(b, zona="tutte")
    b.controlla(pratica(b))
    assert len(pulsanti(b)) == 4  # 3 date + Ignora


def test_solo_provincia(b):
    registra(b, zona="provincia")
    b.controlla(pratica(b))
    assert len(pulsanti(b)) == 3  # MONCALIERI e TORINO, non CUNEO


def test_offerta_scaduta_e_riproposta(b):
    registra(b)
    b.controlla(pratica(b))
    p = pratica(b)
    token = b.offerte[p["id"]]["token"]
    b.offerte[p["id"]]["ts"] -= botmod.TTL_OFFERTA + 1
    b.on_callback(cq(1, f"p:{p['id']}:{token}:0"))
    assert not b.chiamate
    p = pratica(b)
    p["notificati"] = {MEGLIO.key(): time.time() - botmod.TTL_OFFERTA - 1}
    b.store.save(p)
    b.controlla(pratica(b))
    assert p["id"] in b.offerte


def attiva_auto(b, chat=1, giorni=1, n=0):
    p = pratica(b, chat, n)
    b.on_callback(cq(chat, f"auto:{p['id']}:{botmod.versione(p)}:{giorni}"))
    assert b.store.get(p["id"])["auto"] == {"giorni": giorni}


def test_auto_prenota_senza_tocco_nella_stessa_sessione(b):
    registra(b)
    attiva_auto(b)
    b.controlla(pratica(b))
    assert b.chiamate == [(CF, MEGLIO.key(), f"S-{CF}", False)]
    assert not b.offerte
    assert any("conferma automatica" in t and "📅" in t and "OSPEDALE A" in t for t in inviati(b))


def test_auto_rispetta_anticipo_minimo(b, monkeypatch):
    registra(b)
    attiva_auto(b, giorni=7)
    domani = c.Slot(datetime.now() + timedelta(days=1), c.Luogo("OSPEDALE A", "AMB 2", "Via Roma, 1 - TORINO (TO)"), "d")
    monkeypatch.setattr(c, "check", lambda *a: {"attuale": ATT, "slots": [domani], "migliori": [domani], "sessione": "S"})
    b.controlla(pratica(b))
    assert not b.chiamate and pratica(b)["id"] in b.offerte  # troppo presto: solo offerta manuale


def test_auto_un_solo_tentativo_per_data(b, monkeypatch):
    registra(b)
    attiva_auto(b)
    tentativi = []

    def fallisce(*a, **k):
        tentativi.append(1)
        raise c.CupError("Slot non piu' disponibile")
    monkeypatch.setattr(c, "prenota", fallisce)
    b.controlla(pratica(b))
    b.controlla(pratica(b))
    assert len(tentativi) == 1


def test_auto_prende_anche_una_data_gia_offerta_col_pulsante(b):
    registra(b)
    b.controlla(pratica(b))
    b.offerte.clear()  # es. riavvio del bot
    attiva_auto(b)
    b.controlla(pratica(b))
    assert b.chiamate == [(CF, MEGLIO.key(), f"S-{CF}", False)]


def test_auto_si_disattiva_dopo_esito_incerto(b, monkeypatch):
    registra(b)
    attiva_auto(b)
    monkeypatch.setattr(c, "prenota", lambda *a, **k: (_ for _ in ()).throw(
        c.CupError("Conferma inviata, esito incerto: la prenotazione risulta non verificabile.")))
    b.controlla(pratica(b))
    assert pratica(b)["auto"] is None and any("🚨" in t for t in inviati(b))


def test_auto_per_pratica(b):
    registra(b)
    aggiungi_familiare(b)
    attiva_auto(b, n=1)
    io, papa = b.store.della_chat(1)
    assert io.get("auto") is None and papa["auto"] == {"giorni": 1}


def test_cambio_area_di_una_ricetta_attiva(b):
    registra(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/sede"))
    scegli_sede(b, 1, "altro")
    p = pratica(b)
    assert p["stato"] == "attivo" and p["attende_comune"]  # non torna in registrazione
    b.ultimo_msg.clear()
    b.on_message(msg(1, "Moncalieri"))
    p = pratica(b)
    assert p["zona"] == {"tipo": "comune", "valore": "MONCALIERI"} and not p.get("attende_comune")
    b.controlla(p)
    assert pulsanti(b)[0].startswith(f"p:{p['id']}:")  # VICINO, a Moncalieri


def test_prenotazione_non_piu_attiva_mette_in_pausa(b, monkeypatch):
    registra(b)

    def gone(*a):
        raise c.NonAttiva("La prenotazione risulta in stato DISDETTO")
    monkeypatch.setattr(c, "check", gone)
    b.controlla(pratica(b))
    assert pratica(b)["stato"] == "pausa"


def test_data_passata_cancella_la_pratica(b, monkeypatch):
    registra(b)
    passata = c.Prenotazione(datetime.now() - timedelta(days=1), ATT.luogo, ATT.cosa)
    monkeypatch.setattr(c, "check", lambda *a: {"attuale": passata, "slots": [], "migliori": [], "sessione": None})
    b.controlla(pratica(b))
    assert not b.store.della_chat(1)


def test_errore_imprevisto_avvisa_admin_senza_dati(b, monkeypatch):
    registra(b)

    def boom(*a):
        raise KeyError("x")
    monkeypatch.setattr(c, "check", boom)
    b.controlla(pratica(b))
    admin = [d["text"] for m, d in b.out if m == "sendMessage" and d["chat_id"] == "999"]
    assert admin and CF not in admin[0] and NRE not in admin[0]


def test_timeout_isolato_non_avvisa(b, monkeypatch):
    registra(b)

    def timeout(*a):
        raise botmod.requests.ReadTimeout("HTTPSConnectionPool(host='x'): Read timed out.")
    monkeypatch.setattr(c, "check", timeout)
    n = len(inviati(b))
    b.controlla(pratica(b))
    b.controlla(pratica(b))
    assert len(inviati(b)) == n
    b.controlla(pratica(b))
    assert "non risponde (da 3 controlli di fila)" in inviati(b)[-1] and "HTTPSConnectionPool" not in inviati(b)[-1]


def test_controlla_non_a_raffica(b):
    registra(b)
    b.controlla(pratica(b))
    b.offerte.clear()
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/controlla"))
    assert "il prossimo e' possibile" in inviati(b)[-1]


def test_cf_scritto_fuori_registrazione_viene_cancellato(b):
    registra(b)
    b.out.clear()
    b.ultimo_msg.clear()
    b.on_message(msg(1, CF, mid=77))
    assert ("deleteMessage", {"chat_id": 1, "message_id": 77}) in b.out


def test_token_oscurato_nei_log(b):
    b.token = "123:SEGRETO"
    assert "SEGRETO" not in b.redact(Exception("url: /bot123:SEGRETO/getUpdates"))


def test_bot_bloccato_cancella_i_dati(b, monkeypatch):
    registra(b)
    aggiungi_familiare(b)

    class R:
        def json(self):
            return {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}
    monkeypatch.setattr(botmod.requests, "post", lambda *a, **k: R())
    botmod.Bot.tg(b, "sendMessage", chat_id=1, text="ciao")
    assert not b.store.della_chat(1)


def test_luogo_illeggibile_non_si_offre_ne_si_conferma(b, monkeypatch):
    registra(b, zona="tutte")
    vuoto = c.Slot(datetime.now() + timedelta(days=3), c.Luogo("", "", ""), None, proposta=True)
    monkeypatch.setattr(c, "check", lambda *a: {"attuale": ATT, "slots": [vuoto], "migliori": [vuoto], "sessione": "S"})
    n = len(inviati(b))
    b.controlla(pratica(b))
    assert len(inviati(b)) == n
    assert not c.ammesso(vuoto, ATT, "tutte")
    with pytest.raises(c.CupError):
        c._verifica_riepilogo("x", vuoto.quando, "", vuoto, "VISITA - 11.11")


def test_scheduler_salta_chi_ha_un_offerta_aperta(b):
    registra(b)
    aggiungi_familiare(b)
    io, papa = b.store.della_chat(1)
    for p in (io, papa):
        p["prossimo"] = 0
        b.store.save(p)
    b.offerte[io["id"]] = {"token": "t", "ts": time.time(), "sessione": "S", "slots": []}
    assert b.controllo_pianificato()
    assert b.store.get(io["id"])["prossimo"] == 0 and b.store.get(papa["id"])["prossimo"] > time.time()


def test_intervallo_breve_solo_per_admin(b):
    b.admin, b.admin_intervallo = "1", 5
    assert b.intervallo_di(1) == 5 and b.intervallo_di(2) == 45


def test_intervallo_utenti_mai_sotto_il_minimo(tmp_path):
    s = Store(tmp_path / "db.sqlite", Fernet.generate_key().decode())
    assert botmod.Bot(s, "x", intervallo=5).intervallo == botmod.MIN_INTERVALLO
    assert botmod.Bot(s, "x", admin="1", admin_intervallo=1).admin_intervallo == botmod.MIN_INTERVALLO_ADMIN


def test_menu_con_tutti_i_comandi(b):
    b.imposta_menu()
    menu = {d["scope"]["type"]: [x["command"] for x in d["commands"]] for m, d in b.out if m == "setMyCommands"}
    for scope in ("default", "all_private_chats", "chat"):
        assert {"stato", "aggiungi", "help", "privacy"} <= set(menu[scope])
    assert "admin" in menu["chat"] and "admin" not in menu["default"]
    comandi_aiuto = {w[1:].strip(",.") for w in botmod.AIUTO.split() if w.startswith("/")}
    assert {x for x, _ in botmod.COMANDI if x != "help"} <= comandi_aiuto


def test_pulsante_vecchio_non_vale_dopo_modifica(b):
    registra(b)
    vecchio = pratica(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/modifica"))
    p = pratica(b)
    p["creato"] = vecchio["creato"] + 1000  # nuova registrazione
    b.store.save(p)
    b.on_callback(cq(1, f"auto:{p['id']}:{botmod.versione(vecchio)}:1"))
    b.on_callback(cq(1, f"del:{p['id']}:{botmod.versione(vecchio)}:1"))
    assert b.store.get(p["id"]) and not b.store.get(p["id"]).get("auto")


def test_date_senza_seleziona_non_si_offrono(tmp_path, monkeypatch):
    # check() vero, con una sessione finta: la data senza pulsante non e' tra le migliori
    class Finta:
        def __init__(self, cf, nre):
            pass

        def attuale(self):
            return ATT

        def alternative(self, estendi=0):
            return [c.Slot(ATT.quando - timedelta(days=50), ATT.luogo, None, proposta=False), VICINO]
    monkeypatch.setattr(c, "CupSession", Finta)
    res = c.check(CF, NRE, "tutte")
    assert [x.key() for x in res["migliori"]] == [VICINO.key()]


def test_niente_prenotazione_se_i_dati_sono_stati_cancellati(b):
    registra(b)
    attiva_auto(b)
    p = pratica(b)
    b.store.delete(p["id"])  # es. l'utente ha bloccato il bot durante il controllo
    assert b.prenota(p, MEGLIO, "S", automatica=True) == "fallita" and not b.chiamate


def test_auto_fallita_offre_le_altre_date(b, monkeypatch):
    registra(b, zona="tutte")
    attiva_auto(b)
    monkeypatch.setattr(c, "prenota", lambda *a, **k: (_ for _ in ()).throw(c.CupError("Slot non piu' disponibile")))
    b.controlla(pratica(b))
    assert pratica(b)["id"] in b.offerte  # le altre date arrivano col pulsante


def test_richiesta_del_comune_decade(b):
    registra(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/sede"))
    scegli_sede(b, 1, "altro")
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/stato"))  # un comando annulla la richiesta
    b.ultimo_msg.clear()
    b.on_message(msg(1, "grazie"))
    assert pratica(b)["zona"]["tipo"] == "sede" and not pratica(b).get("attende_comune")


def test_nome_non_puo_essere_un_codice_fiscale(b):
    registra(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/aggiungi"))
    b.on_message(msg(1, CF2, mid=20))
    b.on_message(msg(1, NRE2, mid=21))
    b.on_message(msg(1, CF2, mid=22))
    assert pratica(b, 1, 1)["stato"] == "nome"


def test_comune_con_accento(b):
    assert botmod.COMUNE_RE.match("AGLIÈ")


def test_registrazioni_a_meta_non_occupano_posti(b):
    b.max_utenti = 1
    b.on_message(msg(1, "/start"))
    b.on_callback(cq(1, "consenso:1"))  # si ferma al codice fiscale
    registra(b, chat=2)
    assert pratica(b, 2)["stato"] == "attivo"


def test_ricetta_non_piu_trovata_non_resta_occupata(b, monkeypatch):
    registra(b, chat=1)

    def gone(*a):
        raise c.NonTrovata("Non esistono richieste")
    monkeypatch.setattr(c, "check", gone)
    b.controlla(pratica(b))
    registra(b, chat=2)  # stessa ricetta, altra chat: ora si puo'
    assert pratica(b, 2)["stato"] == "attivo"


def pannello(b, chat=1):
    """Ultimo testo del pannello (inviato o modificato sul posto)."""
    return [d["text"] for m, d in b.out if m in ("sendMessage", "editMessageText") and d["text"].startswith("📋")][-1]


def test_pannello_fissato_e_aggiornato_sul_posto(b):
    registra(b)
    creati = [d for m, d in b.out if m == "sendMessage" and d["text"].startswith("📋")]
    assert len(creati) == 1 and any(m == "pinChatMessage" for m, d in b.out)
    b.controlla(pratica(b))
    assert len([d for m, d in b.out if m == "sendMessage" and d["text"].startswith("📋")]) == 1  # modificato, non rimandato
    t = pannello(b)
    assert "📅" in t and "📍 Ospedale A, Via Roma, 1 - Torino (TO)" in t and "🔎 Cerco: solo in questa sede (Ospedale A)" in t
    assert "⚡ Prenoto da solo: no" in t and "⏱" in t and "3 date trovate" in t


def test_pannello_spiega_la_zona_e_il_risultato(b):
    registra(b)
    aggiungi_familiare(b)
    io, fam = b.store.della_chat(1)
    b.controlla(fam)
    t = pannello(b)
    assert "👤 Familiare" in t and "🔎 Cerco: solo nel comune di Alba" in t and "allargo la ricerca" in t
    assert "2 date trovate in Piemonte, 1 a Alba, ✅ 1 prima della tua" in t
    attiva_auto(b, n=1, giorni=3)
    assert "⚡ Prenoto da solo: sì, date da" in pannello(b)


def test_pannello_pulsanti_per_ricetta(b):
    registra(b)
    aggiungi_familiare(b)
    io, fam = b.store.della_chat(1)
    b.aggiorna_pannello(1)
    ultimo = [d for m, d in b.out if m in ("sendMessage", "editMessageText") and d["text"].startswith("📋")][-1]
    cb = [bt["callback_data"] for r in ultimo["reply_markup"]["inline_keyboard"] for bt in r]
    assert f"sc:pausa:{fam['id']}:{botmod.versione(fam)}" in cb and f"sc:sede:{io['id']}:{botmod.versione(io)}" in cb
    b.on_callback(cq(1, f"sc:pausa:{fam['id']}:{botmod.versione(fam)}"))
    assert b.store.get(fam["id"])["stato"] == "pausa" and "⏸ Controlli in pausa" in pannello(b)


def test_regola_in_ogni_avviso(b):
    registra(b)
    b.controlla(pratica(b))
    offerta = [d for m, d in b.out if m == "sendMessage" and d.get("reply_markup") and not d["text"].startswith("📋")][-1]
    assert "🔎 Solo in questa sede" in offerta["text"] and "⚡ decidi tu" in offerta["text"]


def test_stato_rimanda_il_pannello_in_fondo(b):
    registra(b)
    vecchio = b.store.pannello(1)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/stato"))
    assert b.store.pannello(1) != vecchio and ("deleteMessage", {"chat_id": 1, "message_id": vecchio}) in b.out


def test_nomi_leggibili():
    assert botmod.prestazione("TC DEL TORACE E DELL ADDOME SUPERIORE, CON E SENZA MEZZO DI CONTRASTO - 12.34") ==         "TC del torace e dell addome superiore, con e…"
    assert botmod.prestazione("VISITA GENERALE DI CONTROLLO - 11.11") == "Visita generale di controllo"
    assert botmod.indirizzo(c.Luogo("X", "Y", "VIA ESEMPIO 10 - ASTI (AT)")) == "Via Esempio 10 - Asti (AT)"


# --- ricetta mai prenotata: primo appuntamento ------------------------------------------------
def registra_nuova(b, chat=1, zona="tutte", comune=None):
    b.ultimo_msg.clear()
    b.on_message(msg(chat, "/start"))
    b.on_callback(cq(chat, "consenso:1"))
    b.on_message(msg(chat, CF3, mid=30))
    b.on_message(msg(chat, NRE3, mid=31))
    scegli_sede(b, chat, "altro" if comune else zona)
    if comune:
        b.on_message(msg(chat, comune, mid=32))


def test_ricetta_mai_prenotata_si_registra(b):
    registra_nuova(b)
    p = pratica(b)
    assert p["stato"] == "attivo" and botmod.da_prenotare(p)
    assert botmod.attuale_di(p).quando == botmod.SENZA_DATA and botmod.attuale_di(p).cosa == COSA3
    t = "\n".join(inviati(b))
    assert "non e' ancora prenotata" in t and COSA3 in t and "data libera" in t
    # nessuna sede di riferimento: solo "un comune" o "dove propone il CUP"
    scelte = [d for m, d in b.out if m == "sendMessage" and "Dove cerco il primo appuntamento" in d["text"]]
    assert scelte and {x["callback_data"].rsplit(":", 1)[1] for r in scelte[0]["reply_markup"]["inline_keyboard"]
                       for x in r} == {"altro", "tutte"}
    assert "2100" not in t


def test_ricetta_non_valida_non_si_registra(b, monkeypatch):
    def nessuna(cf, nre):
        raise c.NonTrovata("Non esistono prenotazioni")
    monkeypatch.setattr(c, "cerca", nessuna)  # ne' prenotata ne' prenotabile: il portale la rifiuta
    b.on_message(msg(1, "/start"))
    b.on_callback(cq(1, "consenso:1"))
    b.on_message(msg(1, "BRNLCU80A01L219Y", mid=30))
    b.on_message(msg(1, NRE3, mid=31))
    assert any("non accetta questa ricetta" in x for x in inviati(b)) and pratica(b)["stato"] == "cf"


def test_prima_prenotazione_poi_si_anticipa(b):
    registra_nuova(b)
    p = pratica(b)
    b.controlla(p)
    t = inviati(b)[-1]
    assert "C'e' una data libera" in t and "Tocca per prenotare" in t and "2100" not in t
    # ogni data trovata e' buona: la piu' vicina per prima
    [cb, *_] = [x for x in pulsanti(b) if x.startswith("p:")]
    b.on_callback(cq(1, cb))
    assert b.chiamate[-1] == (CF3, NUOVA_CN.key(), "S-" + CF3, False)
    p = pratica(b)
    assert not botmod.da_prenotare(p) and botmod.attuale_di(p).quando == NUOVA_CN.quando
    assert any("Prenotazione fatta" in x for x in inviati(b))
    # da qui e' una prenotazione come le altre: il controllo usa "Sposta"
    b.controlla(pratica(b))
    assert b.zone_viste[-1] == {"tipo": "tutte", "valore": ""} and not botmod.da_prenotare(pratica(b))


def test_ricetta_mai_prenotata_con_comune(b):
    registra_nuova(b, comune="Torino")
    b.controlla(pratica(b))
    date = [x for x in pulsanti(b) if x.startswith("p:")]
    assert len(date) == 1  # solo Torino, non Cuneo
    b.on_callback(cq(1, date[0]))
    assert b.chiamate[-1][1] == NUOVA_TO.key()


def test_prenotata_fuori_dal_bot(b):
    registra_nuova(b)
    b.nuove[CF3] = c.Prenotazione(NUOVA_TO.quando, NUOVA_TO.luogo, COSA3)  # prenotata a mano sul portale
    b.controlla(pratica(b))
    p = pratica(b)
    assert not botmod.da_prenotare(p) and botmod.attuale_di(p).quando == NUOVA_TO.quando
    assert any("risulta prenotata" in x and "Da ora cerco date prima" in x for x in inviati(b))


def test_pannello_ricetta_da_prenotare(b):
    registra_nuova(b)
    testo, _ = b.testo_pannello(1)
    assert "da prenotare: cerco il primo appuntamento" in testo and "2100" not in testo
    b.controlla(pratica(b))
    testo, _ = b.testo_pannello(1)
    assert "2 prenotabili dove cerchi" in testo


def test_auto_prenota_il_primo_appuntamento(b):
    registra_nuova(b)
    p = pratica(b)
    p["auto"] = {"giorni": 1}
    b.store.save(p)
    b.controlla(pratica(b))
    assert b.chiamate and b.chiamate[-1][3] is False and not botmod.da_prenotare(pratica(b))


def test_prenotata_a_mano_durante_la_conferma_automatica(b):
    """Il caso trovato in revisione: la conferma automatica scopre che la ricetta e' appena stata prenotata.
    Le date di quel controllo (e la sua sessione) erano della prenotazione nuova: niente offerte con quelle."""
    registra_nuova(b)
    p = pratica(b)
    p["auto"] = {"giorni": 1}
    b.store.save(p)
    b.prenotata_a_mano = True
    prima = len(b.out)
    b.controlla(pratica(b))
    p = pratica(b)
    assert not botmod.da_prenotare(p) and botmod.attuale_di(p).quando == NUOVA_TO.quando and not p.get("auto")
    dopo = [d["text"] for m, d in b.out[prima:] if m == "sendMessage"]
    assert not any("C'e' una data" in t for t in dopo) and p["id"] not in b.offerte
    assert any("Ho spento la conferma automatica" in t for t in dopo)


def test_piu_prestazioni_alla_registrazione(b, monkeypatch):
    def piu(cf, nre):
        raise c.PiuPrestazioni()
    monkeypatch.setattr(c, "nuova", piu)
    b.on_message(msg(1, "/start"))
    b.on_callback(cq(1, "consenso:1"))
    b.on_message(msg(1, CF3, mid=30))
    b.on_message(msg(1, NRE3, mid=31))
    assert any("piu' prestazioni" in x for x in inviati(b)) and pratica(b)["stato"] == "cf"


def test_ricetta_con_prenotazione_erogata_non_diventa_da_prenotare(b, monkeypatch):
    def erogata(cf, nre):
        raise c.NonAttiva("La prenotazione risulta in stato EROGATO", ["EROGATO"])
    monkeypatch.setattr(c, "cerca", erogata)
    b.on_message(msg(1, "/start"))
    b.on_callback(cq(1, "consenso:1"))
    b.on_message(msg(1, CF3, mid=30))
    b.on_message(msg(1, NRE3, mid=31))
    assert any("non e' attiva" in x for x in inviati(b)) and pratica(b)["stato"] == "cf"
