//! Spostare e disdire una prenotazione, come il bot (telegram-bot/cup_http.py, `prenota` e `disdici`).
//!
//! Fino alla Conferma ogni controllo che non torna ferma tutto e non cambia nulla. Dopo la Conferma (o il "Si'"
//! della disdetta) qualunque problema e' un esito incerto, mai "non fatto": la richiesta puo' essere arrivata al
//! portale anche senza risposta (dal vivo, 2026-09-28: risposta scaduta dopo 180 s, prenotazione fatta). Per questo
//! si verifica sempre con sessioni nuove.

use std::thread;
use std::time::{Duration, Instant};

use chrono::NaiveDateTime;

use crate::analisi::{self, Prenotazione, Slot};
use crate::errore::Errore;
use crate::portale::Sessione;
use crate::zona::{perche_no, Calendario, Zona};

pub const CALL_CENTER: &str = "800 000 500";

/// Il punto in cui e' arrivata un'operazione: chi la chiama salva `Conferma` prima che parta (se non ci riesce,
/// non parte) e cosi', se l'app si ferma a meta', al riavvio sa che l'esito e' incerto.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fase {
    Riepilogo,
    Conferma,
    Verifica,
}

/// Le pause tra le letture di verifica dopo la Conferma: la prima dopo 10 s (il portale ci mette un po'), in tutto
/// non oltre `massimo`, e niente tentativo se resta meno di `minimo`.
#[derive(Debug, Clone)]
pub struct Attese {
    pub pause: Vec<Duration>,
    pub massimo: Duration,
    pub minimo: Duration,
}

impl Default for Attese {
    fn default() -> Self {
        Attese {
            pause: [10, 15, 20, 30, 40].into_iter().map(Duration::from_secs).collect(),
            massimo: Duration::from_secs(180),
            minimo: Duration::from_secs(30),
        }
    }
}

/// Uno spostamento: da dove a dove, e le note del Riepilogo (la preparazione all'esame).
#[derive(Debug, Clone)]
pub struct Spostamento {
    pub prima: Prenotazione,
    pub dopo: Slot,
    pub note: Vec<String>,
    /// false: era una prova, fermata al Riepilogo senza confermare.
    pub fatto: bool,
}

fn no(m: impl Into<String>) -> Errore {
    Errore::Inattesa(m.into())
}

/// Le letture di verifica, con le pause di `attese`: true appena `controlla` trova l'esito voluto. `controlla` riceve
/// quanto puo' aspettare ogni risposta (una lettura dell'elenco ne fa due), cosi' il tutto resta entro `massimo`.
fn verifica(attese: &Attese, mut controlla: impl FnMut(Duration) -> bool) -> bool {
    let fine = Instant::now() + attese.massimo;
    for p in &attese.pause {
        if fine.saturating_duration_since(Instant::now()) < *p + attese.minimo {
            break; // non c'e' piu' tempo per un tentativo sensato
        }
        thread::sleep(*p);
        let rimasto = fine.saturating_duration_since(Instant::now());
        if controlla((rimasto / 2).max(Duration::from_secs(5))) {
            return true;
        }
    }
    false
}

/// Una sessione nuova sul portale; con `Some(attesa)`, ogni risposta si aspetta al massimo tanto.
pub type Nuova<'a> = &'a dyn Fn(Option<Duration>) -> Result<Sessione, Errore>;

/// Sposta la prenotazione sulla data con questa chiave (vedi `Slot::chiave`). `tenuta` e' la sessione del controllo
/// che ha trovato la data: tiene bloccata per noi la proposta, quindi si prova prima con lei; se manca o non va, si
/// riparte con una sessione nuova (`nuova`), come il bot. La data deve andare ancora bene per zona e giorni rispetto
/// alla prenotazione appena riletta dal portale. Con `prova` si ferma al Riepilogo (e accetta anche una data non
/// prima della prenotazione, per provare il percorso). `fase` viene chiamata a ogni passo: se fallisce per
/// `Fase::Conferma`, la Conferma non parte.
#[allow(clippy::too_many_arguments)]
pub fn sposta(
    nuova: Nuova,
    tenuta: Option<Sessione>,
    chiave: &str,
    zona: &Zona,
    cal: &Calendario,
    prova: bool,
    attese: &Attese,
    fase: &mut dyn FnMut(Fase) -> Result<(), String>,
) -> Result<Spostamento, Errore> {
    // l'elenco in una sessione a parte: non tocca lo stato di quella che ha trovato la data
    let mut lista = nuova(None)?;
    let elenco = lista.elenco()?;
    if elenco.n_prenotate > elenco.prenotate.len() {
        return Err(no("Elenco delle prenotazioni non leggibile del tutto: non sposto"));
    }
    let nre = lista.nre().to_string();
    drop(lista);
    let att = elenco.attuale;
    // piu' prestazioni prenotate nello stesso appuntamento: si spostano insieme o niente
    let insieme: Vec<String> =
        elenco.prenotate.iter().filter(|x| x.quando == att.quando).map(|x| x.cosa.clone()).collect();

    let arriva = |s: &mut Sessione| -> Result<(Slot, analisi::Riepilogo), Errore> {
        let mut uguali: Vec<Slot> = s.slots().iter().filter(|x| x.chiave() == chiave).cloned().collect();
        let slot = match uguali.len() {
            0 => return Err(no("La data scelta non è più disponibile")),
            1 => uguali.remove(0),
            n => return Err(no(format!("La data scelta compare {n} volte: non scelgo a caso"))),
        };
        if let Some(motivo) = perche_no(&slot, Some(&att), zona, cal) {
            if !(prova && motivo == "non è prima della prenotazione") {
                return Err(no(format!("La data scelta non va più bene ({motivo}): non sposto")));
            }
        }
        let r = s.riepilogo(&slot)?;
        Ok((slot, r))
    };

    let _ = fase(Fase::Riepilogo);
    // la sessione del controllo vale solo per questa ricetta
    let tenuta = tenuta.filter(|s| s.nre() == nre);
    let primo = match tenuta {
        Some(mut s) => match arriva(&mut s) {
            Ok(x) => Ok((s, x)),
            Err(e) => Err(Some(e.to_string())),
        },
        None => Err(None),
    };
    let (sessione, (slot, riep)) = match primo {
        Ok(x) => x,
        Err(primo) => {
            let mut s = nuova(None)?;
            s.elenco()?;
            s.alternative(zona.estensioni())?;
            match arriva(&mut s) {
                Ok(x) => (s, x),
                Err(e) => {
                    return Err(match primo {
                        Some(p) => no(format!("{e} (prima, nella sessione del controllo: {p})")),
                        None => e,
                    })
                }
            }
        }
    };
    let note = analisi::note_riepilogo(&riep.pagina);
    analisi::verifica_riepilogo(&riep, &slot, &att.cosa)?;
    let nomi = if insieme.len() > 1 { insieme.clone() } else { vec![] };
    if nomi.iter().any(|n| n.trim().is_empty()) {
        return Err(no("Prestazioni prenotate insieme non leggibili dall'elenco: non sposto"));
    }
    if nomi.len() > 1 {
        if !analisi::tutte_nel_riepilogo(&riep.testo, &nomi) {
            return Err(no(
                "Spostare questo appuntamento lo separerebbe dalle altre prestazioni prenotate insieme: non confermo",
            ));
        }
        if !analisi::stesso_appuntamento(&riep.testo, &slot) {
            return Err(no(format!(
                "Il portale mette le prestazioni in appuntamenti diversi (o il Riepilogo non si legge con certezza): \
                 non confermo. Sposta dal sito del CUP o al call center {CALL_CENTER}"
            )));
        }
    }
    if prova {
        return Ok(Spostamento { prima: att, dopo: slot, note, fatto: false });
    }

    // prima della Conferma: se fallisce non e' partito niente
    let view_state = sessione.presa_visione(&riep)?;
    fase(Fase::Conferma).map_err(|_| no("Non riesco a salvare lo stato dello spostamento: non confermo"))?;
    // da qui ogni problema e' "esito incerto": senza risposta (timeout, 504) la Conferma puo' essere arrivata
    let _ = sessione.conferma(&riep, view_state);
    drop(sessione);
    let _ = fase(Fase::Verifica);
    let vecchia = (insieme.len() > 1).then_some(att.quando);
    let mut letta: Option<NaiveDateTime> = None;
    let fatto = verifica(attese, |attesa| {
        let Ok(mut v) = nuova(Some(attesa)) else { return false };
        let Ok(e) = v.elenco() else { return false };
        letta = Some(e.attuale.quando);
        // una riga prenotata senza data leggibile potrebbe essere rimasta alla data vecchia
        let righe = if e.prenotate.is_empty() { vec![e.attuale.clone()] } else { e.prenotate.clone() };
        e.n_prenotate <= e.prenotate.len() && analisi::tutte_al_posto(&righe, &slot, &nomi, vecchia)
    });
    if fatto {
        return Ok(Spostamento { prima: att, dopo: slot, note, fatto: true });
    }
    let stato = letta.map_or("non verificabile".to_string(), |q| format!("al {}", analisi::breve(&q)));
    Err(Errore::Incerto(format!(
        "Conferma inviata, esito incerto: la prenotazione risulta {stato}. Controlla subito sul sito del CUP \
         (Recupera Prenotazioni) o al call center {CALL_CENTER}."
    )))
}

/// Disdice la prenotazione di questa data (e di questo luogo, `Luogo::chiave`, se dato): elenco, "Disdici", dialogo,
/// "Si'", messaggio del portale e, se non basta, verifica con sessioni nuove. Con `prova` si ferma al dialogo, senza
/// il "Si'".
/// Ritorna true se la disdetta e' fatta, false per una prova.
pub fn disdici(
    nuova: Nuova,
    quando: NaiveDateTime,
    luogo: Option<&str>,
    prova: bool,
    attese: &Attese,
    fase: &mut dyn FnMut(Fase) -> Result<(), String>,
) -> Result<bool, Errore> {
    let mut cup = nuova(None)?;
    match cup.elenco() {
        Err(Errore::NonTrovata(_)) => {
            return Err(no("Il portale non mostra prenotazioni per questa ricetta: non c'è niente da disdire"))
        }
        Err(e) => return Err(e),
        Ok(_) => {}
    }
    let si = cup.disdici_dialogo(quando, luogo)?;
    if prova {
        return Ok(false);
    }
    fase(Fase::Conferma).map_err(|_| no("Non riesco a salvare lo stato della disdetta: non disdico"))?;
    let risposta = cup.premi_si(&si).unwrap_or_default();
    drop(cup);
    let _ = fase(Fase::Verifica);
    if analisi::disdetta_riuscita(&risposta) {
        return Ok(true);
    }
    // nessun messaggio (timeout, pagina diversa): si guarda il portale con sessioni nuove
    let mut ancora = false;
    let mut assenze = 0; // "nessun record" di fila: il portale a volte lo dice per prenotazioni che esistono
    let fatta = verifica(attese, |attesa| {
        let Ok(mut v) = nuova(Some(attesa)) else { return false };
        match v.elenco() {
            Ok(e) => {
                assenze = 0;
                ancora = e.prenotate.iter().any(|p| p.quando == quando && luogo.is_none_or(|l| l == p.luogo.chiave()));
                !ancora && e.n_prenotate <= e.prenotate.len()
            }
            // nessuna prenotazione attiva e tutte disdette: e' andata (erogata, in corso... invece non lo prova)
            Err(e @ Errore::NonAttiva { .. }) => e.solo_disdette(),
            Err(Errore::NonTrovata(_)) => {
                assenze += 1;
                assenze >= 2
            }
            Err(_) => false,
        }
    });
    if fatta {
        return Ok(true);
    }
    let stato = if ancora { "ancora presente" } else { "non verificabile" };
    Err(Errore::Incerto(format!(
        "Disdetta inviata, esito incerto: la prenotazione risulta {stato}. Controlla subito sul sito del CUP \
         (Recupera Prenotazioni) o al call center {CALL_CENTER}."
    )))
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::analisi::{Luogo, A, AVANTI, L, RIEPILOGO};
    use crate::prova::{appuntamenti, appuntamento, dialogo, disponibili, elenco, modulo, portale, riepilogo, valore};
    use crate::zona::Dove;

    const CF: &str = "RSSMRA80A01L219X";
    const NRE: &str = "010A00000000001";
    const PRIMA: &str = "Giovedì 7 Gennaio 2027";
    const NUOVA: &str = "Martedì 1 Dicembre 2026";
    /// La data nuova: 1 dicembre 2026 alle 09:00, San Giovanni Bosco, ambulatorio AMB.
    const CHIAVE: &str = "202612010900|SANGIOVANNIBOSCOAMB";
    const PROPOSTA: &str = "202612100900|MOLINETTEAMB";

    type Risposte = Vec<(u16, String)>;

    fn attese() -> Attese {
        Attese { pause: vec![Duration::ZERO; 3], massimo: Duration::from_secs(30), minimo: Duration::ZERO }
    }

    /// GET di Recupera Prenotazioni e risposta alla ricerca con queste righe.
    fn lista(b: &str, righe: &[(&str, &str, &str, &str, &str)]) -> Risposte {
        vec![(200, modulo(L, &format!("{b}/lista"), "")), (200, elenco(righe))]
    }

    fn prenotata(
        quando: &'static str,
        sede: &'static str,
    ) -> [(&'static str, &'static str, &'static str, &'static str, &'static str); 1] {
        [("PRENOTATO", quando, sede, "AMB", "VISITA")]
    }

    /// Una sessione nuova fino alle date: elenco, Sposta, la pagina, i due click e il pannello con la data nuova.
    fn fino_alle_date(b: &str, righe: &[(&str, &str, &str, &str, &str)]) -> Risposte {
        let proposta = appuntamento("Giovedì 10 Dicembre 2026", "MOLINETTE", "AMB", "TORINO (TO)", None);
        let nuova = appuntamento(NUOVA, "SAN GIOVANNI BOSCO", "AMB", "TORINO (TO)", Some(&format!("{A}:s4")));
        let mut r = lista(b, righe);
        r.extend([
            (200, "<partial-response>sposta</partial-response>".into()),
            (200, appuntamenti(&format!("{b}/ricetta"), &proposta)),
            (200, "<partial-response>selettore</partial-response>".into()),
            (200, disponibili(&[nuova], false, false)),
        ]);
        r
    }

    fn ok(testo: &str) -> (u16, String) {
        (200, format!("<partial-response>{testo}</partial-response>"))
    }

    fn riep(b: &str, sede: &str, cose: &[&str], note: bool) -> (u16, String) {
        (200, riepilogo(&format!("{b}/riepilogo"), NUOVA, sede, "AMB", cose, note))
    }

    fn presa_visione_ok() -> (u16, String) {
        ok(
            r#"<i class="icon-check"></i> <span>Presa visione delle note</span><update id="javax.faces.ViewState"><![CDATA[vs2]]></update>"#,
        )
    }

    /// Lo spostamento sul finto portale, senza sessione del controllo: (esito, fasi, richieste ricevute).
    fn prova_sposta(
        risposte: impl FnOnce(&str) -> Risposte,
        tenuta: Option<&dyn Fn(&str) -> Sessione>,
        chiave: &str,
        prova: bool,
        salva: bool,
    ) -> (Result<Spostamento, Errore>, Vec<Fase>, Vec<crate::prova::Vista>) {
        let (base, h) = portale(risposte);
        let nuova = |_: Option<Duration>| Sessione::verso(&base, CF, NRE);
        let tenuta = tenuta.map(|t| t(&base));
        let mut fasi = vec![];
        let esito = sposta(
            &nuova,
            tenuta,
            chiave,
            &Zona::new(Dove::Tutte),
            &Calendario::default(),
            prova,
            &attese(),
            &mut |f| {
                fasi.push(f);
                if f == Fase::Conferma && !salva {
                    return Err("disco pieno".into());
                }
                Ok(())
            },
        );
        (esito, fasi, h.join().unwrap())
    }

    #[test]
    fn sposta_con_sessione_nuova_presa_visione_e_verifica() {
        let (esito, fasi, viste) = prova_sposta(
            |b| {
                let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
                r.extend(fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE")));
                r.extend([ok("selezionata"), riep(b, "SAN GIOVANNI BOSCO", &["VISITA"], true), presa_visione_ok()]);
                r.push(ok("confermata"));
                r.extend(lista(b, &prenotata(NUOVA, "SAN GIOVANNI BOSCO")));
                r
            },
            None,
            CHIAVE,
            false,
            true,
        );
        let s = esito.unwrap();
        assert!(s.fatto);
        assert_eq!((s.dopo.chiave().as_str(), s.prima.luogo.sede.as_str()), (CHIAVE, "MOLINETTE"));
        assert_eq!(s.note, ["Presentarsi a digiuno", "Portare la ricetta"]);
        assert_eq!(fasi, [Fase::Riepilogo, Fase::Conferma, Fase::Verifica]);
        assert_eq!(viste.len(), 14);
        // "Seleziona" della data, con i filtri geografici come il browser
        let sel = &viste[8].2;
        assert_eq!(valore(sel, "javax.faces.source"), Some(format!("{A}:s4")));
        assert_eq!(
            valore(sel, &format!("{A}:localGeoSelectorsavailable:zonaSelectMenuavailable_input")).as_deref(),
            Some("NO_VALUE")
        );
        // "Avanti" a nome del suo modulo
        let avanti = crate::prova::campi(&viste[9].2);
        assert_eq!(avanti[0].0, AVANTI);
        assert_eq!(valore(&viste[9].2, "javax.faces.source"), Some(format!("{AVANTI}:appuntamenti-nextButton-main")));
        // presa visione e Conferma sul modulo del Riepilogo, con i suoi campi; la Conferma col ViewState nuovo
        assert_eq!(viste[10].0, "POST /riepilogo HTTP/1.1");
        assert_eq!(valore(&viste[10].2, "javax.faces.source"), Some(format!("{RIEPILOGO}:_t42")));
        assert_eq!(valore(&viste[10].2, &format!("{RIEPILOGO}:privacy")).as_deref(), Some("si"));
        assert_eq!(valore(&viste[10].2, "javax.faces.ViewState").as_deref(), Some("vs1"));
        assert_eq!(
            valore(&viste[11].2, "javax.faces.source"),
            Some(format!("{RIEPILOGO}:riepilogo-nextButton-bottom"))
        );
        assert_eq!(valore(&viste[11].2, "javax.faces.ViewState").as_deref(), Some("vs2"));
        // la verifica con una sessione nuova
        assert_eq!(viste[12].0, "GET /web/guest/lista-prenotazioni HTTP/1.1");
    }

    #[test]
    fn in_prova_si_ferma_al_riepilogo_e_la_sessione_di_un_altra_ricetta_non_si_usa() {
        let altra = |b: &str| Sessione::verso(b, CF, "010A00000000009").unwrap();
        let (esito, fasi, viste) = prova_sposta(
            |b| {
                let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
                r.extend(fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE")));
                r.extend([ok("selezionata"), riep(b, "SAN GIOVANNI BOSCO", &["VISITA"], true)]);
                r
            },
            Some(&altra),
            CHIAVE,
            true,
            true,
        );
        let s = esito.unwrap();
        assert!(!s.fatto && s.note.len() == 2);
        assert_eq!(fasi, [Fase::Riepilogo]);
        assert_eq!(viste.len(), 10); // niente presa visione, niente Conferma
    }

    #[test]
    fn riepilogo_di_un_altro_luogo_o_senza_stato_salvato_non_conferma() {
        let (esito, fasi, viste) = prova_sposta(
            |b| {
                let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
                r.extend(fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE")));
                r.extend([ok("selezionata"), riep(b, "MAURIZIANO", &["VISITA"], false)]);
                r
            },
            None,
            CHIAVE,
            false,
            true,
        );
        assert!(matches!(esito, Err(Errore::Inattesa(m)) if m.contains("luogo diverso")));
        assert_eq!((fasi.len(), viste.len()), (1, 10));
        // tutto in ordine, ma lo stato "conferma" non si salva: la Conferma non parte
        let (esito, fasi, viste) = prova_sposta(
            |b| {
                let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
                r.extend(fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE")));
                r.extend([ok("selezionata"), riep(b, "SAN GIOVANNI BOSCO", &["VISITA"], false)]);
                r
            },
            None,
            CHIAVE,
            false,
            false,
        );
        assert!(matches!(esito, Err(Errore::Inattesa(m)) if m.contains("non confermo")));
        assert_eq!(fasi, [Fase::Riepilogo, Fase::Conferma]);
        assert_eq!(viste.len(), 10);
    }

    #[test]
    fn la_data_deve_andare_ancora_bene() {
        // la prenotazione intanto e' stata anticipata da un'altra parte: la data scelta non e' piu' prima
        let (esito, _, viste) = prova_sposta(
            |b| {
                let mut r = lista(b, &prenotata("Lunedì 2 Novembre 2026", "MOLINETTE"));
                r.extend(fino_alle_date(b, &prenotata("Lunedì 2 Novembre 2026", "MOLINETTE")));
                r
            },
            None,
            CHIAVE,
            false,
            true,
        );
        assert!(matches!(esito, Err(Errore::Inattesa(m)) if m.contains("non è prima")));
        assert_eq!(viste.len(), 8); // nessun "Seleziona"
                                    // una data che non c'e' piu'
        let (esito, _, _) = prova_sposta(
            |b| {
                let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
                r.extend(fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE")));
                r
            },
            None,
            "202612020900|ALTROAMB",
            false,
            true,
        );
        assert!(matches!(esito, Err(Errore::Inattesa(m)) if m.contains("non è più disponibile")));
    }

    #[test]
    fn prestazioni_prenotate_insieme_si_spostano_tutte_o_niente() {
        let due =
            [("PRENOTATO", PRIMA, "MOLINETTE", "AMB", "VISITA A"), ("PRENOTATO", PRIMA, "MOLINETTE", "AMB", "ESAME B")];
        // il Riepilogo ne riporta una sola: lo spostamento le separerebbe
        let (esito, _, viste) = prova_sposta(
            |b| {
                let mut r = lista(b, &due);
                r.extend(fino_alle_date(b, &due));
                r.extend([ok("selezionata"), riep(b, "SAN GIOVANNI BOSCO", &["VISITA A"], false)]);
                r
            },
            None,
            CHIAVE,
            false,
            true,
        );
        assert!(matches!(esito, Err(Errore::Inattesa(m)) if m.contains("separerebbe")));
        assert_eq!(viste.len(), 10);
        // tutte e due nel Riepilogo: si conferma; la prima verifica ne trova una ancora alla data vecchia, la seconda
        // tutte e due al posto nuovo
        let (esito, _, viste) = prova_sposta(
            |b| {
                let mut r = lista(b, &due);
                r.extend(fino_alle_date(b, &due));
                r.extend([ok("selezionata"), riep(b, "SAN GIOVANNI BOSCO", &["VISITA A", "ESAME B"], false)]);
                r.push(ok("confermata"));
                r.extend(lista(
                    b,
                    &[
                        ("PRENOTATO", NUOVA, "SAN GIOVANNI BOSCO", "AMB", "VISITA A"),
                        ("PRENOTATO", PRIMA, "MOLINETTE", "AMB", "ESAME B"),
                    ],
                ));
                r.extend(lista(
                    b,
                    &[
                        ("PRENOTATO", NUOVA, "SAN GIOVANNI BOSCO", "AMB", "VISITA A"),
                        ("PRENOTATO", NUOVA, "SAN GIOVANNI BOSCO", "AMB", "ESAME B"),
                    ],
                ));
                r
            },
            None,
            CHIAVE,
            false,
            true,
        );
        assert!(esito.unwrap().fatto);
        assert_eq!(viste.len(), 15);
    }

    #[test]
    fn conferma_senza_risposta_si_verifica_lo_stesso_e_data_vecchia_e_esito_incerto() {
        // la risposta alla Conferma non arriva (dal vivo e' successo, e la prenotazione era fatta)
        let (base, h) = portale(|b| {
            let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
            r.extend(fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE")));
            r.extend([ok("selezionata"), riep(b, "SAN GIOVANNI BOSCO", &["VISITA"], false), (0, String::new())]);
            r.extend(lista(b, &prenotata(NUOVA, "SAN GIOVANNI BOSCO")));
            r
        });
        let nuova = |_: Option<Duration>| Sessione::con_attesa(&base, CF, NRE, Duration::from_millis(800));
        let z = Zona::new(Dove::Tutte);
        // la prima verifica dopo che il finto portale ha smesso di aspettare la Conferma (1,5 s)
        let attese = Attese { pause: vec![Duration::from_secs(1)], ..attese() };
        let s = sposta(&nuova, None, CHIAVE, &z, &Calendario::default(), false, &attese, &mut |_| Ok(())).unwrap();
        assert!(s.fatto);
        assert_eq!(h.join().unwrap().len(), 13); // senza note: niente presa visione
                                                 // dopo la Conferma il portale mostra sempre la data vecchia: esito incerto, mai "non spostata"
        let (esito, _, viste) = prova_sposta(
            |b| {
                let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
                r.extend(fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE")));
                r.extend([ok("selezionata"), riep(b, "SAN GIOVANNI BOSCO", &["VISITA"], false), ok("?")]);
                for _ in 0..3 {
                    r.extend(lista(b, &prenotata(PRIMA, "MOLINETTE")));
                }
                r
            },
            None,
            CHIAVE,
            false,
            true,
        );
        assert!(
            matches!(&esito, Err(Errore::Incerto(m)) if m.contains("risulta al gio 07/01/2027 ore 09:00")),
            "{esito:?}"
        );
        assert_eq!(viste.len(), 17);
    }

    #[test]
    fn riusa_la_sessione_del_controllo_per_la_proposta() {
        // il controllo ha aperto "Sposta": la proposta e' tenuta da quella sessione
        let (base, h) = portale(|b| {
            let mut r = fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE"));
            r.extend(lista(b, &prenotata(PRIMA, "MOLINETTE")));
            r.push((
                200,
                riepilogo(
                    &format!("{b}/riepilogo"),
                    "Giovedì 10 Dicembre 2026",
                    "MOLINETTE",
                    "AMB",
                    &["VISITA"],
                    false,
                ),
            ));
            r.push(ok("confermata"));
            r.extend(lista(b, &[("PRENOTATO", "Giovedì 10 Dicembre 2026", "MOLINETTE", "AMB", "VISITA")]));
            r
        });
        let mut tenuta = Sessione::verso(&base, CF, NRE).unwrap();
        tenuta.elenco().unwrap();
        tenuta.alternative(0).unwrap();
        let nuova = |_: Option<Duration>| Sessione::verso(&base, CF, NRE);
        let z = Zona::new(Dove::Tutte);
        let s = sposta(&nuova, Some(tenuta), PROPOSTA, &z, &Calendario::default(), false, &attese(), &mut |_| Ok(()))
            .unwrap();
        assert!(s.fatto && s.dopo.proposta);
        let viste = h.join().unwrap();
        // la proposta e' gia' selezionata: dopo l'elenco, subito "Avanti" nella sessione del controllo
        assert_eq!(viste.len(), 12);
        assert_eq!(viste[8].0, "POST /ricetta HTTP/1.1");
        assert_eq!(valore(&viste[8].2, "javax.faces.source"), Some(format!("{AVANTI}:appuntamenti-nextButton-main")));
    }

    fn prova_disdici(
        risposte: impl FnOnce(&str) -> Risposte,
        luogo: Option<&str>,
        prova: bool,
    ) -> (Result<bool, Errore>, Vec<Fase>, Vec<crate::prova::Vista>) {
        let (base, h) = portale(risposte);
        let nuova = |_: Option<Duration>| Sessione::verso(&base, CF, NRE);
        let quando = analisi::data(&format!("{PRIMA} alle ore 09:00")).unwrap();
        let mut fasi = vec![];
        let esito = disdici(&nuova, quando, luogo, prova, &attese(), &mut |f| {
            fasi.push(f);
            Ok(())
        });
        (esito, fasi, h.join().unwrap())
    }

    #[test]
    fn disdici_col_messaggio_del_portale() {
        let (esito, fasi, viste) = prova_disdici(
            |b| {
                let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
                r.push((200, dialogo(&[("_t289", "Sì"), ("_t290", "No")])));
                r.push(ok("Prenotazione disdetta con successo"));
                r
            },
            None,
            false,
        );
        assert!(esito.unwrap());
        assert_eq!(fasi, [Fase::Conferma, Fase::Verifica]);
        assert_eq!(viste.len(), 4);
        assert_eq!(valore(&viste[2].2, "javax.faces.source"), Some(format!("{L}:righe:0:disdiciButton")));
        assert_eq!(valore(&viste[2].2, "javax.faces.behavior.event").as_deref(), Some("activate"));
        assert_eq!(valore(&viste[2].2, &format!("{L}:IDSearchValueInput")).as_deref(), Some(NRE));
        // il "Si'" letto dal dialogo, con il ViewState che il dialogo ha dato
        assert_eq!(valore(&viste[3].2, "javax.faces.source"), Some(format!("{L}:_t289")));
        assert_eq!(valore(&viste[3].2, "javax.faces.ViewState").as_deref(), Some("vs-dialogo"));
    }

    #[test]
    fn disdici_in_prova_o_con_un_dialogo_non_riconosciuto_non_preme_si() {
        let (esito, fasi, viste) = prova_disdici(
            |b| {
                let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
                r.push((200, dialogo(&[("_t7", "Si'"), ("_t8", "No")])));
                r
            },
            None,
            true,
        );
        assert!(!esito.unwrap() && fasi.is_empty() && viste.len() == 3);
        for pulsanti in [vec![("_t1", "Annulla")], vec![("_t1", "Sì"), ("_t2", "SI")]] {
            let (esito, _, viste) = prova_disdici(
                |b| {
                    let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
                    r.push((200, dialogo(&pulsanti)));
                    r
                },
                None,
                false,
            );
            assert!(matches!(esito, Err(Errore::Inattesa(m)) if m.contains("non riconosciuto")));
            assert_eq!(viste.len(), 3);
        }
    }

    #[test]
    fn disdici_senza_messaggio_verifica_sul_portale() {
        let fino_al_si = |b: &str| {
            let mut r = lista(b, &prenotata(PRIMA, "MOLINETTE"));
            r.push((200, dialogo(&[("_t289", "Sì")])));
            r.push(ok("?"));
            r
        };
        let nessuno = |b: &str| vec![(200, modulo(L, &format!("{b}/lista"), "")), ok("Nessun record trovato")];
        // un "nessun record" isolato non prova la disdetta; due di fila si'
        let (esito, _, viste) = prova_disdici(
            |b| {
                let mut r = fino_al_si(b);
                r.extend(nessuno(b));
                r.extend(nessuno(b));
                r
            },
            None,
            false,
        );
        assert!(esito.unwrap());
        assert_eq!(viste.len(), 8);
        // la prenotazione e' sempre li': esito incerto
        let (esito, _, _) = prova_disdici(
            |b| {
                let mut r = fino_al_si(b);
                for _ in 0..3 {
                    r.extend(lista(b, &prenotata(PRIMA, "MOLINETTE")));
                }
                r
            },
            None,
            false,
        );
        assert!(matches!(&esito, Err(Errore::Incerto(m)) if m.contains("ancora presente")), "{esito:?}");
    }

    #[test]
    fn stessa_ora_in_due_sedi_si_disdice_quella_giusta() {
        let due = [
            ("PRENOTATO", PRIMA, "OSPEDALE A", "AMB 1", "VISITA"),
            ("PRENOTATO", PRIMA, "OSPEDALE B", "AMB 2", "VISITA"),
        ];
        let b_ = Luogo { sede: "OSPEDALE B".into(), ambulatorio: "AMB 2".into(), indirizzo: String::new() };
        let (esito, _, viste) = prova_disdici(
            |b| {
                let mut r = lista(b, &due);
                r.push((200, dialogo(&[("_t5", "Sì")])));
                r.push(ok("Disdetta con successo"));
                r
            },
            Some(&b_.chiave()),
            false,
        );
        assert!(esito.unwrap());
        assert_eq!(valore(&viste[2].2, "javax.faces.source"), Some(format!("{L}:righe:1:disdiciButton")));
        // senza luogo le due righe sono ambigue: non si disdice
        let (esito, _, viste) = prova_disdici(|b| lista(b, &due), None, false);
        assert!(matches!(esito, Err(Errore::Inattesa(m)) if m.contains("unica")));
        assert_eq!(viste.len(), 2);
    }
}
