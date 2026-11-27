"""Un disservizio del portale non e' un cambio del sito, e la riga senza "Sposta" non e' una pagina sconosciuta."""
import time

import pytest

import bot as botmod
import cup_http as c
import impronte
from test_bot import ATT, attiva_auto, b, inviati, pratica, registra, superato  # noqa: F401 (b e' la fixture)

L = c.L
GUSCIO = ('<html><script src="https://www.google.com/recaptcha/api.js"></script><form id="hrefFm"></form>'
          '<div class="captcha-box"></div><input name="g-recaptcha-token"></html>')


# --- riconoscere la pagina -----------------------------------------------------------------------
@pytest.mark.parametrize("form", [c.L, c.R])
def test_pagina_d_ingresso_senza_modulo_e_non_disponibile(form):
    with pytest.raises(c.NonDisponibile) as e:
        c._Form(None, GUSCIO, form)
    assert e.value.pagina == ("elenco" if form == c.L else "ricerca")


@pytest.mark.parametrize("segno", ['<div class="g-recaptcha"></div>', '<div class="h-captcha"></div>',
                                   '<script src="https://challenges.cloudflare.com/x.js"></script>'])
def test_pagina_senza_modulo_con_verifica_vera_e_antibot(segno):
    with pytest.raises(c.Antibot):
        c._Form(None, GUSCIO + segno, c.L)


def test_modulo_presente_ma_senza_campi_e_un_cambio():
    with pytest.raises(c.Cambiato) as e:
        c._Form(None, f'<form id="{c.L}"></form>', c.L)
    assert not isinstance(e.value, c.NonDisponibile)


# --- disservizio nel bot -------------------------------------------------------------------------
def guasto(monkeypatch, passo="elenco", pagina="elenco"):
    def f(*a, **k):
        c._passo(passo)
        c.RICHIESTE.append((time.time(), passo, 0.2, "ok", 200))  # il portale risponde 200: non basta
        raise c.NonDisponibile(pagina)
    monkeypatch.setattr(c, "check", f)


def sano(monkeypatch, vero, pagina="elenco"):
    def f(*a, **k):
        c.LETTE.add(pagina)
        return superato(vero)(*a, **k)
    monkeypatch.setattr(c, "check", f)


def avvisi_utente(b, pezzo):
    return [t for t in inviati(b) if pezzo in t]


def segnalate(b, *livelli):
    return b.store.segnalazioni(livelli=livelli or (impronte.URGENTE, impronte.RIPRESA, "errore"))


def test_guasto_breve_nessun_allarme_e_nessuna_sospensione(b, monkeypatch):
    registra(b)
    attiva_auto(b)
    guasto(monkeypatch)
    b.controlla(pratica(b))
    b.controlla(pratica(b))  # due ricette controllate a pochi secondi: non basta
    b.segnala_pagine_giu()
    assert not segnalate(b) and not b.auto_sospeso() and not avvisi_utente(b, "Non e' un problema del bot")


def test_guasto_prolungato_un_avviso_solo_senza_sospendere(b, monkeypatch):
    registra(b)
    attiva_auto(b)
    guasto(monkeypatch)
    b.controlla(pratica(b))
    b.controlla(pratica(b))
    dopo = time.time() + botmod.GIU_DOPO + 1
    b.segnala_pagine_giu(dopo)  # dal ciclo del bot, senza un controllo in piu'
    b.segnala_pagine_giu(dopo + 60)
    [s] = segnalate(b)
    assert s["livello"] == "errore" and "guasto prolungato o cambio del sito" in s["testo"]
    assert "== Stack ==" in s["rapporto"] and not b.auto_sospeso()
    assert len(avvisi_utente(b, "Non e' un problema del bot")) == 1


def test_un_solo_controllo_fallito_non_basta(b, monkeypatch):
    registra(b)
    guasto(monkeypatch)
    b.controlla(pratica(b))
    b.segnala_pagine_giu(time.time() + botmod.GIU_DOPO + 1)
    assert not segnalate(b)


def test_guasto_che_rientra_prima_non_dice_nulla(b, monkeypatch):
    registra(b)
    vero = c.check
    guasto(monkeypatch)
    b.controlla(pratica(b))
    b.controlla(pratica(b))
    sano(monkeypatch, vero)
    b.controlla(pratica(b))
    b.segnala_pagine_giu(time.time() + botmod.GIU_DOPO + 1)
    assert not segnalate(b) and not b.pagine_giu()


def test_ritorno_dopo_l_avviso_detto_una_volta(b, monkeypatch):
    registra(b)
    vero = c.check
    guasto(monkeypatch)
    b.controlla(pratica(b))
    b.controlla(pratica(b))
    b.segnala_pagine_giu(time.time() + botmod.GIU_DOPO + 1)
    b.avvisa_utenti_ripresa()  # un'altra ripresa (es. rete) non dice "funziona" con la pagina ancora giu'
    assert not avvisi_utente(b, "funziona di nuovo")
    sano(monkeypatch, vero)
    b.controlla(pratica(b))
    b.controlla(pratica(b))
    assert len(avvisi_utente(b, "funziona di nuovo")) == 1
    assert [s["livello"] for s in segnalate(b)] == [impronte.RIPRESA, "errore"]


def test_un_200_o_la_pagina_diversa_non_chiudono_il_guasto(b, monkeypatch):
    registra(b)
    vero = c.check
    guasto(monkeypatch)
    b.controlla(pratica(b))
    sano(monkeypatch, vero, pagina="ricerca")  # ha letto l'altra pagina
    b.controlla(pratica(b))
    assert b.pagine_giu()["elenco"]["n"] == 1


def test_i_30_minuti_sopravvivono_al_riavvio(b, monkeypatch):
    registra(b)
    guasto(monkeypatch)
    b.controlla(pratica(b))
    b.controlla(pratica(b))
    nuovo = botmod.Bot(b.store, "x", admin="999", distanza=0)
    nuovo.tg = b.tg
    nuovo.segnala_pagine_giu(time.time() + botmod.GIU_DOPO + 1)
    assert len(segnalate(b)) == 1


def test_un_guasto_vecchio_non_si_somma_a_uno_nuovo(b):
    ora = time.time()
    for quando in (ora - botmod.GIU_VECCHIO - 100, ora):
        b.pagina_giu("elenco", "elenco", c.NonDisponibile("elenco"), "", ora=quando)
    assert b.pagine_giu()["elenco"]["n"] == 1 and not segnalate(b)


def test_guasto_dimenticato_dopo_un_giorno(b):
    ora = time.time()
    for quando in (ora, ora + 1, ora + botmod.GIU_DOPO + 1):
        b.pagina_giu("elenco", "elenco", c.NonDisponibile("elenco"), "", ora=quando)
    b.segnala_pagine_giu(ora + botmod.GIU_DOPO + botmod.GIU_SCADUTO + 10)
    assert not b.pagine_giu() and not b.store.valore("disservizio_utenti")


def test_in_prenotazione_resta_subito_urgente(b):
    c.PASSO = "conferma"
    b.osserva_pagine(c.NonDisponibile("elenco"))
    assert b.auto_sospeso() and segnalate(b, impronte.URGENTE)


def test_antibot_subito_una_volta(b):
    c.PASSO = "elenco"
    b.osserva_pagine(c.Antibot("Il portale chiede una verifica antibot"))
    b.osserva_pagine(c.Antibot("Il portale chiede una verifica antibot"))
    [s] = segnalate(b, impronte.URGENTE)
    assert "antibot" in s["testo"] and b.auto_sospeso()


def test_antibot_dopo_pagine_sconosciute_si_dice_lo_stesso(b):
    c.PASSO = "elenco"
    for _ in range(3):
        b.osserva_pagine(c.Cambiato("Pagina CUP inattesa"))
    for _ in range(2):
        b.osserva_pagine(c.Antibot("Il portale chiede una verifica antibot"))
    assert sorted("antibot" in s["testo"] for s in segnalate(b, impronte.URGENTE)) == [False, True]


def test_al_ritorno_un_messaggio_solo(b, monkeypatch):
    registra(b)
    vero = c.check
    guasto(monkeypatch)
    for _ in range(botmod.AVVISA_ERRORI[0]):
        b.controlla(pratica(b))
    b.segnala_pagine_giu(time.time() + botmod.GIU_DOPO + 1)
    sano(monkeypatch, vero)
    b.out.clear()
    b.controlla(pratica(b))
    assert len(avvisi_utente(b, "funziona di nuovo")) == 1 and not avvisi_utente(b, "risponde di nuovo")


def test_un_guasto_interrompe_la_serie_di_pagine_sconosciute(b):
    c.PASSO = "elenco"
    for e in (c.Cambiato("Pagina CUP inattesa"), c.NonDisponibile("elenco"), c.Cambiato("Pagina CUP inattesa")):
        b.osserva_pagine(e)
    assert not segnalate(b, impronte.URGENTE)


def test_antibot_senza_amministratore_non_riempie_il_database(b):
    b.admin = None
    c.PASSO = "elenco"
    for _ in range(4):
        b.osserva_pagine(c.Antibot("Il portale chiede una verifica antibot"))
    assert len(segnalate(b, impronte.URGENTE)) == 1


# --- "Sposta" che manca --------------------------------------------------------------------------
def elenco_xml(sposta_in_riga=None):
    righe = ""
    for i, stato in enumerate(("PRENOTATO", "EROGATO")):
        righe += f'<span>Stato:</span> {stato} <span>ROSSI MARIO 010A00000000001</span><div id="{L}:righe:{i}:disdiciButton">Disdici</div>'
        if i == sposta_in_riga:
            righe += f'<div id="{L}:righe:{i}:spostaButton">Sposta appuntamento</div>'
    return righe


def test_diagnosi_senza_dati_e_ammessa_nei_rapporti():
    d = c.diagnosi_sposta(elenco_xml(sposta_in_riga=1), 0)
    assert d == ("senza Sposta: righe 2, stati [prenotato, erogato], riga attuale 0, "
                 "pulsanti [disdiciButton@0, disdiciButton@1, spostaButton@1], scritta Sposta [no, si]")
    assert impronte.diario_sicuro([d]) == [d]
    assert "ROSSI" not in d and "010A" not in d


def test_alternative_senza_pulsante_non_preme_nulla():
    s = c.CupSession.__new__(c.CupSession)
    s.lista_xml, s.riga = elenco_xml(), 0
    with pytest.raises(c.SenzaSposta) as e:
        s.alternative()  # senza sessione di rete: se premesse qualcosa fallirebbe in altro modo
    assert "spostaButton" not in e.value.diagnosi


def test_check_senza_sposta_legge_la_prenotazione(monkeypatch):
    class Finta:
        prenotate, n_prenotate, note_attuale = [], 0, None

        def __init__(self, *a):
            pass

        def attuale(self):
            return ATT

        def alternative(self, estendi=0):
            raise c.SenzaSposta("senza Sposta: x")
    monkeypatch.setattr(c, "CupSession", Finta)
    res = c.check("cf", "nre", {"tipo": "tutte", "valore": ""})
    assert res["attuale"] == ATT and res["slots"] == [] and res["senza_sposta"] == "senza Sposta: x"


def senza_sposta(monkeypatch, sposta_in_riga=None):
    def f(cf, nre, zona):
        return {"attuale": ATT, "slots": [], "migliori": [], "sessione": "S",
                "senza_sposta": c.diagnosi_sposta(elenco_xml(sposta_in_riga), 0)}
    monkeypatch.setattr(c, "check", f)


def test_senza_sposta_un_messaggio_con_data_ora_luogo(b, monkeypatch):
    registra(b)
    senza_sposta(monkeypatch)
    b.out.clear()
    b.controlla(pratica(b))
    b.controlla(pratica(b))
    [m] = avvisi_utente(b, "non trovo il pulsante «Sposta appuntamento»")
    assert botmod.fmt(ATT.quando) in m and ATT.luogo.sede in m and "senza premerlo" in m
    p = pratica(b)
    assert p["stato"] == "attivo" and p["errori"] == 0 and p["senza_sposta"]
    assert "non trovo il pulsante" in b.riassunto(p)
    assert not b.auto_sospeso() and p["id"] not in b.sessioni
    [s] = segnalate(b)
    assert s["livello"] == "errore" and "non vedo pulsanti Sposta" in s["testo"]
    assert "senza Sposta: righe 2" in s["rapporto"] and "ROSSI" not in s["rapporto"]
    b.controlla(pratica(b), manuale=True)  # chiesto a mano: si risponde
    assert len(avvisi_utente(b, "non trovo il pulsante «Sposta appuntamento»")) == 2


def test_senza_sposta_toglie_le_date_del_controllo_di_prima(b, monkeypatch):
    registra(b)
    b.controlla(pratica(b))
    assert pratica(b)["viste"]
    senza_sposta(monkeypatch)
    b.controlla(pratica(b))
    assert pratica(b)["viste"] == []


def test_senza_sposta_con_un_pulsante_non_associato_e_possibile_errore_nostro(b, monkeypatch):
    registra(b)
    senza_sposta(monkeypatch, sposta_in_riga=1)
    b.controlla(pratica(b))
    [s] = segnalate(b)
    assert "possibile errore del bot" in s["testo"]


def test_sposta_che_va_e_viene_non_ripete_il_messaggio(b, monkeypatch):
    registra(b)
    vero = c.check
    senza_sposta(monkeypatch)
    b.controlla(pratica(b))
    monkeypatch.setattr(c, "check", vero)
    b.controlla(pratica(b))
    assert "senza_sposta" not in pratica(b)
    senza_sposta(monkeypatch)
    b.controlla(pratica(b))
    assert len(avvisi_utente(b, "non trovo il pulsante «Sposta appuntamento»")) == 1
    assert len(segnalate(b)) == 1
