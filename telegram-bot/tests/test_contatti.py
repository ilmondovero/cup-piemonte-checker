"""Contatti facoltativi per il CUP (email e cellulare): registrazione in chat, /contatti, Mini App e campi del
Riepilogo del portale. Mai i valori in chiaro nei log, nel diario, nelle schede o nei messaggi."""
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot as botmod  # noqa: E402
import cup_http as c  # noqa: E402
import impronte as im  # noqa: E402
from test_bot import CF, CF2, NRE, NRE2, b, cq, inviati, msg, pratica, pulsanti, registra, scegli_sede  # noqa: E402,F401
from test_webapp import app, cerca_e_attendi, get, post  # noqa: E402,F401
from test_nuova_http import FATTA, RIEP, SLOT, sessioni_finte  # noqa: E402

EMAIL, CELL = "mario.rossi@gmail.com", "3487654321"
EMAIL_M, CELL_M = "m***@gmail.com", "*** *** 4321"


def cancellati(b):
    return {d["message_id"] for m, d in b.out if m == "deleteMessage"}


def fino_ai_contatti(b, chat=1, cf=CF, nre=NRE):
    b.ultimo_msg.clear()
    b.on_message(msg(chat, "/start"))
    b.on_callback(cq(chat, "consenso:1"))
    b.on_message(msg(chat, cf, mid=10))
    b.on_message(msg(chat, nre, mid=11))


def nessun_valore(testi):
    t = "\n".join(testi)
    for dato in (EMAIL, "mario.rossi", CELL, "348 765 4321", "7654321"):
        assert dato not in t, dato


# --- validazione e maschere ---------------------------------------------------------------
def test_validazione_e_maschere():
    assert botmod.norm_email("  Mario.Rossi@GMail.com ") == EMAIL
    for cattiva in ("mario", "mario@", "@gmail.com", "mario@gmail", "ma rio@@x.it", "a..b@x.it", "x" * 95 + "@gmail.com",
                    "mario rossi@gmail.com", "mario@gm ail.com", ".mario@gmail.com", "mario.@gmail.com",
                    "mario@-gmail.com", "mario@gmail-.com", "mario@gmail.c"):
        assert botmod.norm_email(cattiva) is None, cattiva
    assert botmod.norm_email("a@x.it") == "a@x.it" and botmod.norm_email("m.r+cup@posta-x.example.it")
    for buono in ("333 123 4567", "+39 333.123.4567", "0039 (333) 123-4567", "333123456", "3331234567"):
        assert botmod.norm_cellulare(buono) in ("3331234567", "333123456"), buono
    for cattivo in ("011 1234567", "33312", "3331234567890", "+44 7911 123456", "333-abc-4567",
                    "333" + " " * 40 + "1234567", "333 123 4567" + "-" * 30):  # troppo lungo: niente tagli prima
        assert botmod.norm_cellulare(cattivo) is None, cattivo
    assert botmod.maschera_email(EMAIL) == EMAIL_M and botmod.maschera_cellulare("3331234567") == "*** *** 4567"
    assert botmod.maschera_email("a@x.it") == "*@x.it"
    assert botmod.descr_contatti({"email": EMAIL, "cellulare": CELL}) == f"{EMAIL_M} · {CELL_M}"
    assert botmod.descr_contatti({}) == "nessuno"


def test_pulisci_toglie_email_e_cellulari():
    for dato in (EMAIL, CELL, "333 123 4567", "+39 333 123 4567", "333.123.4567", "(+39) 333-123-4567", "333123456"):
        t = im.pulisci(f"errore per {dato}.")
        assert "1234" not in t and "mario" not in t, (dato, t)


# --- chat: registrazione ---------------------------------------------------------------------
def test_registrazione_con_contatti_cancella_i_messaggi(b, caplog):
    caplog.set_level(logging.INFO)
    fino_ai_contatti(b)
    assert pratica(b)["stato"] == "email" and any("conferma e il promemoria" in t for t in inviati(b))
    assert any(":salta:" in x for x in pulsanti(b))
    b.on_message(msg(1, "mario@", mid=12))  # non valida: si richiede, con Salta
    assert pratica(b)["stato"] == "email" and "non sembra un indirizzo email" in inviati(b)[-1].lower()
    assert any(":salta:" in x for x in pulsanti(b))
    b.on_message(msg(1, "Mario.Rossi@Gmail.com", mid=13))
    assert pratica(b)["stato"] == "cellulare" and pratica(b)["email"] == EMAIL
    assert any("SMS del CUP" in t for t in inviati(b))
    b.on_message(msg(1, "011 123456", mid=14))  # un fisso: si richiede
    assert pratica(b)["stato"] == "cellulare" and "cellulare italiano" in inviati(b)[-1]
    b.on_message(msg(1, "+39 348.765-4321", mid=15))
    p = pratica(b)
    assert p["cellulare"] == CELL and p["stato"] == "sede"
    assert {12, 13, 14, 15} <= cancellati(b)  # i contatti non restano nella chat
    scegli_sede(b, 1, "sede")
    assert pratica(b)["stato"] == "attivo"
    assert any(EMAIL_M in t for t in inviati(b)) and any(CELL_M in t for t in inviati(b))
    nessun_valore(inviati(b) + [r.getMessage() for r in caplog.records])


def test_registrazione_salta_i_contatti(b):
    fino_ai_contatti(b)
    pid, v = pratica(b)["id"], botmod.versione(pratica(b))
    assert f"ct:{pid}:{v}:salta:email" in pulsanti(b)
    b.on_callback(cq(1, f"ct:{pid}:{v}:salta:email"))
    assert pratica(b)["stato"] == "cellulare"
    b.on_callback(cq(1, f"ct:{pid}:{v}:salta:email"))  # il Salta dell'email, toccato di nuovo: non salta il cellulare
    assert pratica(b)["stato"] == "cellulare"
    b.on_callback(cq(2, f"ct:{pid}:{v}:salta:cellulare"))  # un'altra chat: niente
    assert pratica(b)["stato"] == "cellulare"
    b.on_callback(cq(1, f"ct:{pid}:{v}:salta:cellulare"))
    p = pratica(b)
    assert p["stato"] == "sede" and "email" not in p and "cellulare" not in p and p["contatti_chiesti"]
    b.on_callback(cq(1, f"ct:{pid}:{v}:salta:cellulare"))  # doppio tocco: non torna indietro
    assert pratica(b)["stato"] == "sede"


def test_modifica_non_richiede_i_contatti(b):
    registra(b)
    b.esegui("modifica", pratica(b))
    b.on_message(msg(1, CF2, mid=50))
    b.on_message(msg(1, NRE2, mid=51))
    assert pratica(b)["stato"] == "sede"  # gia' chiesti per questa ricetta: si cambiano con /contatti


def test_registrazione_ripresa_richiede_il_contatto(b):
    fino_ai_contatti(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/aggiungi"))  # ancora in registrazione
    assert "Completa prima" in "\n".join(inviati(b)[-2:]) and "conferma e il promemoria" in inviati(b)[-1]


def test_seconda_ricetta_usa_gli_stessi_contatti(b):
    fino_ai_contatti(b)
    b.on_message(msg(1, EMAIL, mid=12))
    b.on_message(msg(1, CELL, mid=13))
    scegli_sede(b, 1, "sede")
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/aggiungi"))
    b.on_message(msg(1, CF2, mid=20))
    b.on_message(msg(1, NRE2, mid=21))
    fam = pratica(b, 1, 1)
    [stessi] = [x for x in pulsanti(b) if ":stessi:" in x]
    offerta = [d for m, d in b.out if m == "sendMessage" and d.get("reply_markup")][-1]
    assert f"{EMAIL_M} · {CELL_M}" in str(offerta["reply_markup"]) and EMAIL not in str(offerta)
    b.on_callback(cq(1, stessi))
    fam = b.store.get(fam["id"])
    assert fam["email"] == EMAIL and fam["cellulare"] == CELL and fam["stato"] == "nome"


def test_prima_ricetta_senza_contatti_niente_stessi(b):
    registra(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/aggiungi"))
    b.on_message(msg(1, CF2, mid=20))
    b.on_message(msg(1, NRE2, mid=21))
    assert not any(":stessi:" in x for x in pulsanti(b)) and any(":salta:" in x for x in pulsanti(b))


# --- chat: /contatti e /dati ------------------------------------------------------------------
def con_contatti(b):
    fino_ai_contatti(b)
    b.on_message(msg(1, EMAIL, mid=12))
    b.on_message(msg(1, CELL, mid=13))
    scegli_sede(b, 1, "sede")
    return pratica(b)


def test_contatti_cambia_e_togli(b, caplog):
    caplog.set_level(logging.INFO)
    p = con_contatti(b)
    pv = f"{p['id']}:{botmod.versione(p)}"
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/contatti"))
    assert EMAIL_M in inviati(b)[-1] and CELL_M in inviati(b)[-1]
    assert {f"ct:{pv}:email", f"ct:{pv}:cell", f"ct:{pv}:viaemail", f"ct:{pv}:viacell"} == set(pulsanti(b))
    b.on_callback(cq(1, f"ct:{pv}:email"))
    b.ultimo_msg.clear()
    b.on_message(msg(1, "sbagliata", mid=30))
    assert 30 in cancellati(b) and "riscrivilo" in inviati(b)[-1] and pratica(b).get("attende_contatto")
    b.ultimo_msg.clear()
    b.on_message(msg(1, "Anna@Esempio.it", mid=31))
    p = pratica(b)
    assert 31 in cancellati(b) and p["email"] == "anna@esempio.it" and "attende_contatto" not in p
    assert "a***@esempio.it" in inviati(b)[-1]
    b.on_callback(cq(1, f"ct:{pv}:viacell"))
    assert "cellulare" not in pratica(b) and pratica(b)["email"] == "anna@esempio.it"
    b.on_callback(cq(1, f"ct:{pv}:viaemail"))
    assert not botmod.contatti_di(pratica(b))
    b.on_callback(cq(1, "ct:%s:000000:viaemail" % p["id"]))  # pulsante di un messaggio vecchio
    assert "messaggio vecchio" in inviati(b)[-1]
    nessun_valore(inviati(b) + [r.getMessage() for r in caplog.records])


def test_contatti_richiesta_decade_con_un_comando(b):
    p = con_contatti(b)
    b.on_callback(cq(1, f"ct:{p['id']}:{botmod.versione(p)}:cell"))
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/stato"))
    assert "attende_contatto" not in pratica(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "3471112222", mid=40))
    assert pratica(b)["cellulare"] == CELL  # non era piu' una risposta
    assert 40 in cancellati(b) and "per i contatti usa /contatti" in inviati(b)[-1]


def test_contatto_decaduto_il_messaggio_dopo_si_cancella_lo_stesso(b, monkeypatch):
    p = con_contatti(b)
    b.on_callback(cq(1, f"ct:{p['id']}:{botmod.versione(p)}:email"))
    vero = botmod.time.time
    monkeypatch.setattr(botmod.time, "time", lambda: vero() + botmod.ATTESA_COMUNE + 60)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "ciao, come va", mid=41))
    assert 41 in cancellati(b) and "attende_contatto" not in pratica(b) and pratica(b)["email"] == EMAIL


def test_email_scritta_fuori_da_ogni_richiesta_si_cancella(b):
    registra(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, " Mario.Rossi@gmail.com ", mid=42))
    assert 42 in cancellati(b) and inviati(b)[-1] == "Ho cancellato il messaggio: per i contatti usa /contatti."
    assert "email" not in pratica(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "ciao", mid=43))
    assert 43 not in cancellati(b)


def test_contatti_non_si_cambiano_durante_una_registrazione(b):
    p = con_contatti(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/aggiungi"))
    b.on_callback(cq(1, f"ct:{p['id']}:{botmod.versione(p)}:email"))
    assert "Completa prima la registrazione" in inviati(b)[-1] and "attende_contatto" not in pratica(b)


def test_dati_mostra_i_contatti_mascherati(b):
    con_contatti(b)
    b.ultimo_msg.clear()
    b.on_message(msg(1, "/dati"))
    assert f"Contatti per il CUP: {EMAIL_M} · {CELL_M}" in inviati(b)[-1]
    nessun_valore(inviati(b))


def test_contatti_in_menu_aiuto_e_informativa():
    assert "contatti" in [x for x, _ in botmod.COMANDI] and "/contatti" in botmod.AIUTO
    assert "email e un cellulare" in botmod.PRIVACY and "cifrati" in botmod.PRIVACY


# --- chat: prenotazione -------------------------------------------------------------------------
def test_prenotazione_passa_i_contatti_e_li_dice_mascherati(b, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    con_contatti(b)
    passati = []

    def prenota(cf, nre, slot, contatti=None, **k):
        passati.append(contatti)
        c.CONTATTI.update(email=True, cellulare=True)
        return "Prenotazione spostata."
    monkeypatch.setattr(c, "prenota", prenota)
    b.controlla(pratica(b))
    b.on_callback(cq(1, pulsanti(b)[0]))
    assert passati == [{"email": EMAIL, "cellulare": CELL}]
    [ok] = [t for t in inviati(b) if t.startswith("✅")]
    assert f"Il CUP mandera' la conferma a {EMAIL_M} e gli SMS al {CELL_M}." in ok
    nessun_valore(inviati(b) + [r.getMessage() for r in caplog.records])


def test_prenotazione_contatti_rifiutati_lo_dice(b, monkeypatch):
    con_contatti(b)

    def prenota(cf, nre, slot, **k):
        c.CONTATTI.update(email=True, cellulare=False, rifiutati=True)  # partita solo l'email
        return "Prenotazione spostata."
    monkeypatch.setattr(c, "prenota", prenota)
    b.controlla(pratica(b))
    b.on_callback(cq(1, pulsanti(b)[0]))
    [ok] = [t for t in inviati(b) if t.startswith("✅")]
    assert "non ha accettato l'email: ho confermato senza" in ok and "cellulare" not in ok and "mandera'" not in ok


def test_prenotazione_senza_contatti_come_prima(b):
    registra(b)
    b.controlla(pratica(b))
    b.on_callback(cq(1, pulsanti(b)[0]))
    assert b.contatti == [{}]
    [ok] = [t for t in inviati(b) if t.startswith("✅")]
    assert "mandera'" not in ok and "Arriveranno SMS/email dal CUP" in ok


# --- portale: campi del Riepilogo ----------------------------------------------------------------
R = c.RIEPILOGO


def riepilogo(note=False, email=True, tel3=True, sola_lettura=False):
    campi = (f'<input class="dis" disabled="true" id="{R}:tel1" name="{R}:tel1" type="text" value="0111111111">'
             f'<input class="dis" disabled="true" id="{R}:tel2" name="{R}:tel2" type="text" value="3209999999">' +
             (f'<input class="iceInpTxt" id="{R}:tel3" name="{R}:tel3"{" readonly" if sola_lettura else ""} '
              'type="text" value="3478888888">' if tel3 else "") +
             (f'<input class="iceInpTxt" id="{R}:email" name="{R}:email" type="text" value="">' if email else ""))
    bottone = (f'<div id="{R}:_t217" onmouseover="y"><span aria-describedby="Conferma presa visione"></span></div>'
               if note else "")
    return (f'<form id="{R}" method="post"><input type="hidden" name="{R}" value="{R}" />'
            f'<input type="hidden" name="javax.faces.encodedURL" value="{c.CUP}/riep" />'
            f'<input type="hidden" name="ice.window" value="w1" /><input type="hidden" name="ice.view" value="v1" />'
            f'{campi}{bottone}<div id="{R}:riepilogo-nextButton-bottom">Conferma</div>'
            f'<input type="hidden" name="javax.faces.ViewState" value="vs1" /></form>')


def risposta(errore=None, successo=False, vs=None, presa=False, generico=False):
    msg_ = (f'<div class="{"alert alert-danger" if generico else "messagifyMsg alert-danger"}"><span>{errore}</span>'
            '</div>' if errore else "")
    ok = "Prenotazione conclusa con successo" if successo else ""
    pv = '<i class="icon-check" style="m"></i><span id="a">Presa visione delle note</span>' if presa else ""
    stato = f'<update id="j_id1:javax.faces.ViewState:0"><![CDATA[{vs}]]></update>' if vs else ""
    return f'<partial-response><changes>{stato}<update id="m"><![CDATA[{msg_}{ok}{pv}]]></update></changes></partial-response>'


class FormFinto:
    inviati, risposte = [], []

    def __init__(self, s, page, form):
        self.vs = "vs1"

    def post(self, campi, form=None):
        FormFinto.inviati.append((dict(campi), self.vs))
        return FormFinto.risposte.pop(0)


def sessione(monkeypatch, *risposte, contatti=None):
    FormFinto.inviati, FormFinto.risposte = [], list(risposte)
    monkeypatch.setattr(c, "_Form", FormFinto)
    s = c.CupSession(CF, NRE)
    s.contatti = {"email": EMAIL, "cellulare": CELL} if contatti is None else contatti
    c.DIARIO.clear()
    return s


def test_disabilitati_partono_come_prima_ma_non_ricevono_contatti():
    page = ('<form id="F"><input type="text" name="a" disabled="true" value="1"><input type="text" name="b" disabled>'
            '<input type="text" name="c" aria-disabled="true" value="3"></form>')
    assert c._form_fields(page, "F") == {"a": "1", "b": "", "c": "3"}  # come sempre: anche i disabilitati
    assert c._modificabili(page, "F") == {"c"}


def test_contatti_mai_in_un_campo_disabilitato(monkeypatch):
    page = riepilogo(tel3=False).replace(f'name="{R}:email" type="text"', f'name="{R}:email" disabled="true" type="text"')
    s = sessione(monkeypatch, risposta(successo=True))
    s.prepara_contatti(page)
    s.conferma(page)
    [(campi, _)] = FormFinto.inviati
    assert campi[f"{R}:tel1"] == "0111111111" and campi[f"{R}:tel2"] == "3209999999" and campi[f"{R}:email"] == ""
    assert CELL not in campi.values() and EMAIL not in campi.values()
    assert list(c.DIARIO) == ["contatti: email no (nessun campo), cellulare no (nessun campo)"]


def test_riepilogo_email_nel_suo_campo_cellulare_nel_primo_telefono_libero(monkeypatch):
    s = sessione(monkeypatch, risposta(successo=True))
    s.prepara_contatti(riepilogo())
    s.conferma(riepilogo())
    [(campi, _)] = FormFinto.inviati
    assert campi[f"{R}:email"] == EMAIL and campi[f"{R}:tel3"] == CELL  # al posto del numero del portale
    assert campi[f"{R}:tel1"] == "0111111111" and campi[f"{R}:tel2"] == "3209999999"  # disabilitati: invariati
    assert list(c.DIARIO) == ["contatti: email si, cellulare si (campo tel3)"]
    assert c.CONTATTI == {"email": True, "cellulare": True}
    assert im.diario_sicuro(c.DIARIO) == list(c.DIARIO)


def test_riepilogo_senza_contatti_come_lo_compila_il_portale(monkeypatch):
    s = sessione(monkeypatch, risposta(successo=True), contatti={})
    s.prepara_contatti(riepilogo())
    s.conferma(riepilogo())
    [(campi, _)] = FormFinto.inviati
    assert campi[f"{R}:tel3"] == "3478888888" and campi[f"{R}:email"] == "" and campi[f"{R}:tel1"] == "0111111111"
    assert not c.DIARIO and not c.CONTATTI


def test_riepilogo_senza_campi_liberi(monkeypatch):
    s = sessione(monkeypatch, risposta(successo=True))
    s.prepara_contatti(riepilogo(email=False, tel3=False))
    s.conferma(riepilogo(email=False, tel3=False))
    [(campi, _)] = FormFinto.inviati
    assert EMAIL not in campi.values() and CELL not in campi.values() and campi[f"{R}:tel2"] == "3209999999"
    assert list(c.DIARIO) == ["contatti: email no (nessun campo), cellulare no (nessun campo)"]
    assert im.diario_sicuro(c.DIARIO) == list(c.DIARIO) and c.CONTATTI == {"email": False, "cellulare": False}


def test_conferma_contatti_rifiutati_si_riprova_una_volta_senza(monkeypatch):
    s = sessione(monkeypatch, risposta("Formato dell'indirizzo email non valido", vs="vs2"), risposta(successo=True))
    s.prepara_contatti(riepilogo())
    assert "conclusa con successo" in s.conferma(riepilogo())
    (prima, _), (seconda, vs) = FormFinto.inviati
    assert prima[f"{R}:email"] == EMAIL and seconda[f"{R}:email"] == "" and seconda[f"{R}:tel3"] == "3478888888"
    assert vs == "vs2" and c.CONTATTI == {"email": True, "cellulare": True, "rifiutati": True}  # erano partiti
    assert c.DIARIO[-1] == "contatti: rifiutati dal portale, confermo senza"
    assert im.diario_sicuro(c.DIARIO) == list(c.DIARIO)
    d = " | ".join(c.DIARIO)
    assert EMAIL not in d and CELL not in d


def test_conferma_rifiutata_due_volte_non_si_riprova_ancora(monkeypatch):
    s = sessione(monkeypatch, risposta("Telefono non valido"), risposta("Telefono non valido"))
    s.prepara_contatti(riepilogo())
    s.conferma(riepilogo())
    assert len(FormFinto.inviati) == 2


@pytest.mark.parametrize("errore", ["***email***: Formato non valido", "***tel3***: Valore non ammesso",
                                    "Il numero di telefono non e' corretto", "Indirizzo di posta non valido"])
def test_conferma_errori_sui_contatti_si_riprova(monkeypatch, errore):
    s = sessione(monkeypatch, risposta(errore), risposta(successo=True))
    s.prepara_contatti(riepilogo())
    s.conferma(riepilogo())
    assert len(FormFinto.inviati) == 2 and c.CONTATTI["rifiutati"]


@pytest.mark.parametrize("prima", [
    risposta("Recapito email da verificare", successo=True),  # riuscita: mai una seconda Conferma
    risposta("Ricetta non valida"),  # errore che non riguarda i contatti
    risposta("Formato data non corretto"),
    risposta("Errore, contattare il call center telefonando al 800 000 500"),
    risposta("Errore sul telefono: contattare l'assistenza"),
    risposta("Formato email non valido", generico=True),  # riquadro rosso generico, non del campo
    risposta()])
def test_conferma_niente_secondo_invio(monkeypatch, prima):
    s = sessione(monkeypatch, prima)
    s.prepara_contatti(riepilogo())
    s.conferma(riepilogo())
    assert len(FormFinto.inviati) == 1 and not c.CONTATTI.get("rifiutati")


def test_presa_visione_contatti_rifiutati_si_riprova_senza(monkeypatch):
    s = sessione(monkeypatch, risposta("Email non valida"), risposta(presa=True, vs="vs3"), risposta(successo=True))
    page = riepilogo(note=True)
    s.prepara_contatti(page)
    s.presa_visione(page)
    s.conferma(page)
    (prima, _), (seconda, _), (conferma, vs) = FormFinto.inviati
    assert prima[f"{R}:email"] == EMAIL and seconda[f"{R}:email"] == "" and conferma[f"{R}:tel3"] == "3478888888"
    assert vs == "vs3" and len(FormFinto.inviati) == 3


def test_presa_visione_ripetuta_tiene_il_viewstate_piu_recente(monkeypatch):
    # la risposta che rifiuta i contatti ha un ViewState nuovo, quella della seconda presa visione no
    s = sessione(monkeypatch, risposta("Email non valida", vs="vs2"), risposta(presa=True), risposta(successo=True))
    page = riepilogo(note=True)
    s.prepara_contatti(page)
    s.presa_visione(page)
    s.conferma(page)
    assert [vs for _, vs in FormFinto.inviati] == ["vs1", "vs2", "vs2"] and s.vs_riepilogo == "vs2"


def test_contatti_mai_in_un_campo_readonly(monkeypatch):
    s = sessione(monkeypatch, risposta(successo=True), contatti={"cellulare": CELL})
    s.prepara_contatti(riepilogo(sola_lettura=True))
    s.conferma(riepilogo(sola_lettura=True))
    [(campi, _)] = FormFinto.inviati
    assert campi[f"{R}:tel3"] == "3478888888" and CELL not in campi.values()
    assert list(c.DIARIO) == ["contatti: email no, cellulare no (nessun campo)"]


def test_prenota_porta_i_contatti_nel_riepilogo(monkeypatch):
    sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])

    def al_riepilogo(self, slot):  # come quello di sessioni_finte, ma con la pagina e i suoi campi
        m = c.DATE_RE.search(RIEP)
        return RIEP, c._date(m.group(0)), RIEP[m.end():m.end() + 250], riepilogo()
    monkeypatch.setattr(c.CupSession, "riepilogo", al_riepilogo)
    campi = []
    monkeypatch.setattr(c.CupSession, "conferma", lambda self, page: campi.append(self._campi_riepilogo(page)))
    c.DIARIO.clear()
    assert c.prenota(CF, NRE, SLOT, zona={"tipo": "tutte", "valore": ""}, dry_run=False, nuova=True,
                     contatti={"email": EMAIL, "cellulare": CELL}) == "Prenotazione fatta."
    assert campi[0][f"{R}:email"] == EMAIL and campi[0][f"{R}:tel3"] == CELL
    assert "contatti: email si, cellulare si (campo tel3)" in c.DIARIO
    assert all(EMAIL not in x and CELL not in x for x in c.DIARIO)


# --- Mini App -----------------------------------------------------------------------------
CF3, NRE3 = "BNCLRA70B41F205Z", "010A00000000033"


def test_app_nuova_ricetta_con_contatti(app):
    _, _, corpo = get(app, "/ui/nuova", chat=3)
    t = corpo.decode()
    assert 'name="email"' in t and 'name="cellulare"' in t and 'name="stessi"' not in t
    (stato, _, corpo), _ = cerca_e_attendi(app, 3, {"cf": CF3, "nre": NRE3, "consenso": "1",
                                                    "email": "Mario.Rossi@gmail.com", "cellulare": "+39 348 765 4321"})
    [p] = app.store.della_chat(3)
    assert p["email"] == EMAIL and p["cellulare"] == CELL
    nessun_valore([corpo.decode()])


@pytest.mark.parametrize("campo, valore, errore", [("email", "mario@", "email non sembra valida"),
                                                    ("cellulare", "011 1234567", "cellulare non sembra italiano")])
def test_app_contatti_non_validi(app, campo, valore, errore):
    stato, _, corpo = post(app, "/ui/nuova", {"cf": CF3, "nre": NRE3, "consenso": "1", campo: valore}, chat=3)
    t = corpo.decode()
    assert errore in t and app.bot.coda.empty() and valore not in t  # mai riproposto in chiaro


def test_app_usa_gli_stessi_contatti(app):
    io = pratica(app.bot, 1)
    io.update(email=EMAIL, cellulare=CELL)
    app.store.save(io)
    _, _, corpo = get(app, "/ui/nuova")
    t = corpo.decode()
    assert 'name="stessi"' in t and EMAIL_M in t and CELL_M in t
    nessun_valore([t])
    cerca_e_attendi(app, 1, {"cf": CF3, "nre": NRE3, "stessi": "1"})
    nuova = app.store.della_chat(1)[-1]
    assert nuova["cf"] == CF3 and nuova["email"] == EMAIL and nuova["cellulare"] == CELL


def test_app_cambia_e_togli_i_contatti(app):
    pid = pratica(app.bot, 1)["id"]
    stato, _, corpo = post(app, f"/ui/r/{pid}/contatti", {"email": "anna@esempio.it", "cellulare": "347 111 2222"})
    p = app.store.get(pid)
    assert stato == 200 and p["email"] == "anna@esempio.it" and p["cellulare"] == "3471112222"
    assert "anna@" not in corpo.decode() and "a***@esempio.it" in corpo.decode()
    _, _, corpo = get(app, f"/ui/r/{pid}/altro")
    t = corpo.decode()
    assert "a***@esempio.it" in t and "*** *** 2222" in t and "anna@" not in t and "3471112222" not in t
    assert 'value="email"' in t and 'value="cellulare"' in t  # i pulsanti per toglierli
    _, _, corpo = get(app, "/ui/dati")
    assert "a***@esempio.it · *** *** 2222" in corpo.decode() and "anna@" not in corpo.decode()
    post(app, f"/ui/r/{pid}/contatti", {"togli": "cellulare"})
    p = app.store.get(pid)
    assert "cellulare" not in p and p["email"] == "anna@esempio.it"
    assert post(app, f"/ui/r/{pid}/contatti", {"email": "anna"})[0] == 400
    assert post(app, f"/ui/r/{pid}/contatti", {})[0] == 400
    assert post(app, f"/ui/r/{pid}/contatti", {"email": EMAIL}, chat=2)[0] == 404  # ricetta di un altro
    assert post(app, f"/ui/r/{pid}/contatti", {"email": EMAIL}, auth_date=__import__("time").time() - 3 * 3600)[0] == 401
    assert app.store.get(pid)["email"] == "anna@esempio.it"
