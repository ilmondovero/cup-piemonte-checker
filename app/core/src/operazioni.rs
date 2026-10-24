//! Spostare e disdire una prenotazione, o prenotare una ricetta mai prenotata, come il bot
//! (telegram-bot/cup_http.py, `prenota` e `disdici`).
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
use crate::portale::{Modo, Sessione};
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
    /// Prima di una prenotazione nuova: se il portale dice "nessuna prenotazione", la pausa prima di rileggere
    /// (a volte lo dice anche per ricette prenotate).
    pub rilettura: Duration,
}

impl Default for Attese {
    fn default() -> Self {
        Attese {
            pause: [10, 15, 20, 30, 40].into_iter().map(Duration::from_secs).collect(),
            massimo: Duration::from_secs(180),
            minimo: Duration::from_secs(30),
            rilettura: Duration::from_secs(3),
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
    // la sessione del controllo vale solo per questa ricetta e per "Sposta"
    let tenuta = tenuta.filter(|s| s.nre() == nre && s.modo() == Modo::Sposta);
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

/// Con piu' prestazioni si prenota solo la data che il portale propone per tutte insieme.
pub const SOLO_PROPOSTA: &str = "Con più prestazioni prenoto solo la data proposta dal portale per tutte insieme: \
                                 questa non è la data proposta";

/// La prima prenotazione di una ricetta: la data, le prestazioni del carrello e le note del Riepilogo.
#[derive(Debug, Clone)]
pub struct Prenotata {
    pub dopo: Slot,
    pub prestazioni: Vec<String>,
    pub note: Vec<String>,
    /// false: era una prova, fermata al Riepilogo senza confermare.
    pub fatto: bool,
}

/// "Nel frattempo la ricetta risulta prenotata al ...".
fn gia(p: &Prenotazione) -> Errore {
    Errore::GiaPrenotata(format!("Nel frattempo la ricetta risulta prenotata al {}", analisi::breve(&p.quando)))
}

/// La ricetta e' ancora da prenotare: il portale non trova prenotazioni (due volte, con sessioni diverse: a volte
/// sbaglia) o le ha tutte disdette. Una prenotazione erogata o in corso non si affianca con un'altra.
fn ancora_da_prenotare(nuova: Nuova, attese: &Attese) -> Result<String, Errore> {
    let mut s = nuova(None)?;
    let nre = s.nre().to_string();
    let lettura = |r: Result<analisi::Elenco, Errore>| match r {
        Ok(e) => Err(gia(&e.attuale)),
        Err(e) if e.solo_disdette() => Ok(true),
        Err(e @ Errore::NonAttiva { .. }) => Err(no(format!("{e}: non prenoto, verifica sul sito del CUP"))),
        Err(Errore::NonTrovata(_)) => Ok(false),
        Err(e) => Err(e),
    };
    // "nessun record": a volte il portale lo dice per ricette prenotate, quindi si rilegge con una sessione nuova
    if lettura(s.elenco())? {
        return Ok(nre);
    }
    thread::sleep(attese.rilettura);
    lettura(nuova(None)?.elenco())?;
    Ok(nre)
}

/// Prenota una ricetta mai prenotata sulla data con questa chiave (vedi `Slot::chiave`), come `sposta`: prima con la
/// sessione del controllo che ha trovato la data (`tenuta`, se e' di una prenotazione nuova della stessa ricetta),
/// se no con una sessione nuova. Prima di tutto rilegge l'elenco: se nel frattempo la ricetta risulta prenotata,
/// `GiaPrenotata`. Il Riepilogo deve avere la data, il luogo e tutte le prestazioni del carrello in un solo
/// appuntamento; con piu' prestazioni si prenota solo la data proposta. Con `prova` si ferma al Riepilogo.
#[allow(clippy::too_many_arguments)]
pub fn prenota(
    nuova: Nuova,
    tenuta: Option<Sessione>,
    chiave: &str,
    zona: &Zona,
    cal: &Calendario,
    prova: bool,
    attese: &Attese,
    fase: &mut dyn FnMut(Fase) -> Result<(), String>,
) -> Result<Prenotata, Errore> {
    let nre = ancora_da_prenotare(nuova, attese)?;
    let tenuta = tenuta.filter(|s| s.nre() == nre && s.modo() == Modo::Nuova);
    // prima di aprire altre sessioni: non bloccare date per niente
    if tenuta
        .as_ref()
        .is_some_and(|s| s.n_prestazioni() > 1 && s.slots().iter().any(|x| x.chiave() == chiave && !x.proposta))
    {
        return Err(no(SOLO_PROPOSTA));
    }

    let arriva = |s: &mut Sessione| -> Result<(Slot, analisi::Riepilogo), Errore> {
        let mut uguali: Vec<Slot> = s.slots().iter().filter(|x| x.chiave() == chiave).cloned().collect();
        let slot = match uguali.len() {
            0 => return Err(no("La data scelta non è più disponibile")),
            1 => uguali.remove(0),
            n => return Err(no(format!("La data scelta compare {n} volte: non scelgo a caso"))),
        };
        if let Some(motivo) = perche_no(&slot, None, zona, cal) {
            return Err(no(format!("La data scelta non va più bene ({motivo}): non prenoto")));
        }
        if s.n_prestazioni() > 1 && !slot.proposta {
            return Err(no(SOLO_PROPOSTA)); // prima di "Seleziona", che bloccherebbe la data
        }
        let r = s.riepilogo(&slot)?;
        Ok((slot, r))
    };

    let _ = fase(Fase::Riepilogo);
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
            s.cerca_nuova(zona.estensioni())?;
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
    let nomi = sessione.prestazioni().to_vec();
    // il carrello di un passo precedente ne aveva di piu': il portale ne prenoterebbe solo una parte
    if nomi.is_empty() || nomi.len() != sessione.n_prestazioni() || !nomi.iter().all(|n| analisi::nome_leggibile(n)) {
        return Err(no("Prestazioni della ricetta non riconosciute con certezza: non confermo"));
    }
    analisi::verifica_riepilogo(&riep, &slot, &nomi[0])?;
    if nomi.len() > 1 {
        if !analisi::tutte_nel_riepilogo(&riep.testo, &nomi) {
            return Err(no("Il Riepilogo non riporta tutte le prestazioni della ricetta: non confermo"));
        }
        if !analisi::stesso_appuntamento(&riep.testo, &slot) {
            return Err(no(format!(
                "Il portale mette le prestazioni in appuntamenti diversi (o il Riepilogo non si legge con certezza): \
                 non confermo. Prenota dal sito del CUP o al call center {CALL_CENTER}"
            )));
        }
    } else {
        // una sola prestazione nel carrello, ma il Riepilogo ne dichiara altre: il portale ne ha nascoste
        if analisi::dichiarate(&riep.testo).is_some_and(|n| n != 1) {
            return Err(no("Il Riepilogo riporta più prestazioni del carrello: non confermo"));
        }
        if analisi::tutte_le_date(&riep.testo).iter().any(|q| *q != slot.quando) {
            return Err(no("Il Riepilogo contiene più appuntamenti: non confermo"));
        }
    }
    if prova {
        return Ok(Prenotata { dopo: slot, prestazioni: nomi, note, fatto: false });
    }

    // prima della Conferma: se fallisce non e' partito niente
    let view_state = sessione.presa_visione(&riep)?;
    fase(Fase::Conferma).map_err(|_| no("Non riesco a salvare lo stato della prenotazione: non confermo"))?;
    // da qui ogni problema e' "esito incerto": senza risposta (timeout, 504) la Conferma puo' essere arrivata
    let _ = sessione.conferma(&riep, view_state);
    drop(sessione);
    let _ = fase(Fase::Verifica);
    let mut letta: Option<NaiveDateTime> = None;
    let fatto = verifica(attese, |attesa| {
        let Ok(mut v) = nuova(Some(attesa)) else { return false };
        let Ok(e) = v.elenco() else { return false };
        letta = Some(e.attuale.quando);
        let righe = if e.prenotate.is_empty() { vec![e.attuale.clone()] } else { e.prenotate.clone() };
        e.n_prenotate <= e.prenotate.len() && analisi::tutte_al_posto(&righe, &slot, &nomi, None)
    });
    if fatto {
        return Ok(Prenotata { dopo: slot, prestazioni: nomi, note, fatto: true });
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
    use crate::prova::{
        appuntamenti, appuntamento, carrello, dialogo, disponibili, elenco, modulo, passo_prestazioni, portale,
        ricerca, riepilogo, risposta, valore,
    };
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
        Attese {
            pause: vec![Duration::ZERO; 3],
            massimo: Duration::from_secs(30),
            minimo: Duration::ZERO,
            rilettura: Duration::ZERO,
        }
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

    const ECO: &str = "ECOGRAFIA ADDOME COMPLETO";
    const GIORNO: &str = "Lunedì 6 Settembre 2027";
    /// La data libera: 6 settembre 2027 alle 09:00, OSPEDALE, ambulatorio ECO 2; la proposta e' il 14 settembre.
    const LIBERA: &str = "202709060900|OSPEDALEECO2";

    /// Recupera Prenotazioni: nessuna prenotazione per la ricetta.
    fn nessuna(b: &str) -> Risposte {
        vec![(200, modulo(L, &format!("{b}/lista"), "")), ok("Non esistono richieste per il paziente")]
    }

    /// Una sessione nuova fino alle date di una ricetta mai prenotata: Ricerca, Prestazioni, appuntamenti.
    fn date_nuove(b: &str, nomi: &[&str]) -> Risposte {
        let az = format!("{b}/ricetta");
        let proposta = appuntamento("Martedì 14 Settembre 2027", "POLIAMBULATORIO NORD", "ECO 1", "TORINO (TO)", None);
        let libera = appuntamento(GIORNO, "OSPEDALE", "ECO 2", "ASTI (AT)", Some(&format!("{A}:s1")));
        vec![
            (200, ricerca(&az)),
            (200, risposta(None)),
            (200, passo_prestazioni(&az, nomi)),
            (200, risposta(None)),
            (200, format!("{}{}", carrello(&az, nomi), appuntamenti(&az, &proposta))),
            ok("selettore"),
            (200, disponibili(&[proposta, libera], false, false)),
        ]
    }

    fn riep_nuova(b: &str, cose: &[&str], note: bool) -> (u16, String) {
        (200, riepilogo(&format!("{b}/riepilogo"), GIORNO, "OSPEDALE", "ECO 2", cose, note))
    }

    /// La prenotazione sul finto portale: (esito, fasi, richieste ricevute).
    fn prova_prenota(
        risposte: impl FnOnce(&str) -> Risposte,
        tenuta: Option<&dyn Fn(&str) -> Sessione>,
        chiave: &str,
        prova: bool,
    ) -> (Result<Prenotata, Errore>, Vec<Fase>, Vec<crate::prova::Vista>) {
        let (base, h) = portale(risposte);
        let nuova = |_: Option<Duration>| Sessione::verso(&base, CF, NRE);
        let tenuta = tenuta.map(|t| t(&base));
        let mut fasi = vec![];
        let z = Zona::new(Dove::Tutte);
        let esito = prenota(&nuova, tenuta, chiave, &z, &Calendario::default(), prova, &attese(), &mut |f| {
            fasi.push(f);
            Ok(())
        });
        (esito, fasi, h.join().unwrap())
    }

    #[test]
    fn prima_prenotazione_letta_due_volte_confermata_e_verificata() {
        let (esito, fasi, viste) = prova_prenota(
            |b| {
                let mut r = nessuna(b);
                r.extend(nessuna(b));
                r.extend(date_nuove(b, &[ECO]));
                r.extend([ok("selezionata"), riep_nuova(b, &[ECO], true), presa_visione_ok(), ok("confermata")]);
                r.extend(lista(b, &[("PRENOTATO", GIORNO, "OSPEDALE", "ECO 2", ECO)]));
                r
            },
            None,
            LIBERA,
            false,
        );
        let p = esito.unwrap();
        assert!(p.fatto && p.dopo.chiave() == LIBERA && !p.dopo.proposta);
        assert_eq!((p.prestazioni.as_slice(), p.note.len()), ([ECO.to_string()].as_slice(), 2));
        assert_eq!(fasi, [Fase::Riepilogo, Fase::Conferma, Fase::Verifica]);
        assert_eq!(viste.len(), 17);
        // "Seleziona" della data, poi "Avanti", presa visione e Conferma sul Riepilogo
        assert_eq!(valore(&viste[11].2, "javax.faces.source"), Some(format!("{A}:s1")));
        assert_eq!(valore(&viste[12].2, "javax.faces.source"), Some(format!("{AVANTI}:appuntamenti-nextButton-main")));
        assert_eq!(
            valore(&viste[14].2, "javax.faces.source"),
            Some(format!("{RIEPILOGO}:riepilogo-nextButton-bottom"))
        );
        assert_eq!(viste[15].0, "GET /web/guest/lista-prenotazioni HTTP/1.1");
    }

    #[test]
    fn gia_prenotata_o_erogata_non_si_prenota() {
        // l'utente l'ha prenotata dal sito del CUP: niente Ricerca, niente date bloccate
        let (esito, fasi, viste) =
            prova_prenota(|b| lista(b, &[("PRENOTATO", GIORNO, "OSPEDALE", "ECO 2", ECO)]), None, LIBERA, false);
        assert!(matches!(&esito, Err(Errore::GiaPrenotata(m)) if m.contains("lun 06/09/2027 ore 09:00")), "{esito:?}");
        assert!(fasi.is_empty() && viste.len() == 2);
        // "nessuna prenotazione" la prima volta, ma la seconda lettura la trova
        let (esito, _, viste) = prova_prenota(
            |b| {
                let mut r = nessuna(b);
                r.extend(lista(b, &[("PRENOTATO", GIORNO, "OSPEDALE", "ECO 2", ECO)]));
                r
            },
            None,
            LIBERA,
            false,
        );
        assert!(matches!(esito, Err(Errore::GiaPrenotata(_))) && viste.len() == 4);
        // erogata: non si aggiunge un appuntamento
        let (esito, _, viste) =
            prova_prenota(|b| lista(b, &[("EROGATO", GIORNO, "OSPEDALE", "ECO 2", ECO)]), None, LIBERA, false);
        assert!(matches!(&esito, Err(Errore::Inattesa(m)) if m.contains("non prenoto")), "{esito:?}");
        assert_eq!(viste.len(), 2);
    }

    #[test]
    fn disdetta_si_prenota_di_nuovo_e_in_prova_si_ferma_al_riepilogo() {
        let (esito, fasi, viste) = prova_prenota(
            |b| {
                let mut r = lista(b, &[("DISDETTO", GIORNO, "OSPEDALE", "ECO 2", ECO)]);
                r.extend(date_nuove(b, &[ECO]));
                r.extend([ok("selezionata"), riep_nuova(b, &[ECO], true)]);
                r
            },
            None,
            LIBERA,
            true,
        );
        let p = esito.unwrap();
        assert!(!p.fatto && p.note.len() == 2);
        assert_eq!(fasi, [Fase::Riepilogo]);
        assert_eq!(viste.len(), 11); // niente presa visione, niente Conferma
    }

    #[test]
    fn riepilogo_che_non_torna_non_conferma() {
        for (cose, attesa) in [
            (vec![ECO, "ALTRO ESAME"], "più prestazioni del carrello"),
            (vec!["VISITA DIVERSA"], "prestazione della ricetta"),
        ] {
            let (esito, fasi, viste) = prova_prenota(
                |b| {
                    let mut r = nessuna(b);
                    r.extend(nessuna(b));
                    r.extend(date_nuove(b, &[ECO]));
                    r.extend([ok("selezionata"), riep_nuova(b, &cose, false)]);
                    r
                },
                None,
                LIBERA,
                false,
            );
            assert!(matches!(&esito, Err(Errore::Inattesa(m)) if m.contains(attesa)), "{esito:?}");
            assert_eq!((fasi.len(), viste.len()), (1, 13));
        }
        // due appuntamenti nel Riepilogo
        let (esito, _, _) = prova_prenota(
            |b| {
                let mut r = nessuna(b);
                r.extend(nessuna(b));
                r.extend(date_nuove(b, &[ECO]));
                let (c, p) = riep_nuova(b, &[ECO], false);
                let doppia = p.replace(
                    "<input type=\"hidden\" name",
                    "<span>Quando:</span><span>Martedì 7 Settembre 2027</span><span>alle ore</span><span>10:00</span>\
                     <input type=\"hidden\" name",
                );
                r.extend([ok("selezionata"), (c, doppia)]);
                r
            },
            None,
            LIBERA,
            false,
        );
        assert!(matches!(&esito, Err(Errore::Inattesa(m)) if m.contains("più appuntamenti")), "{esito:?}");
    }

    #[test]
    fn piu_prestazioni_si_prenotano_solo_sulla_proposta_e_tutte_insieme() {
        let due = [ECO, "VISITA CARDIOLOGICA"];
        // la sessione del controllo ha visto due prestazioni: una data non proposta si rifiuta subito
        let controllo = |b: &str| {
            let mut s = Sessione::verso(b, CF, NRE).unwrap();
            s.cerca_nuova(0).unwrap();
            s
        };
        let (esito, fasi, viste) = prova_prenota(
            |b| {
                let mut r = date_nuove(b, &due);
                r.extend(nessuna(b));
                r.extend(nessuna(b));
                r
            },
            Some(&controllo),
            LIBERA,
            false,
        );
        assert!(matches!(&esito, Err(Errore::Inattesa(m)) if m == SOLO_PROPOSTA), "{esito:?}");
        assert!(fasi.is_empty() && viste.len() == 11);
        // la proposta, nella sessione del controllo: subito "Avanti" (e' gia' selezionata), tutte e due nel Riepilogo
        let proposta = "202709140900|POLIAMBULATORIONORDECO1";
        let (esito, _, viste) = prova_prenota(
            |b| {
                let mut r = date_nuove(b, &due);
                r.extend(nessuna(b));
                r.extend(nessuna(b));
                r.push((
                    200,
                    riepilogo(
                        &format!("{b}/riepilogo"),
                        "Martedì 14 Settembre 2027",
                        "POLIAMBULATORIO NORD",
                        "ECO 1",
                        &due,
                        false,
                    ),
                ));
                r.push(ok("confermata"));
                r.extend(lista(
                    b,
                    &[
                        ("PRENOTATO", "Martedì 14 Settembre 2027", "POLIAMBULATORIO NORD", "ECO 1", ECO),
                        (
                            "PRENOTATO",
                            "Martedì 14 Settembre 2027",
                            "POLIAMBULATORIO NORD",
                            "ECO 1",
                            "VISITA CARDIOLOGICA",
                        ),
                    ],
                ));
                r
            },
            Some(&controllo),
            proposta,
            false,
        );
        let p = esito.unwrap();
        assert!(p.fatto && p.dopo.proposta && p.prestazioni.len() == 2);
        assert_eq!(viste.len(), 15);
        assert_eq!(valore(&viste[11].2, "javax.faces.source"), Some(format!("{AVANTI}:appuntamenti-nextButton-main")));
    }

    #[test]
    fn la_sessione_di_sposta_non_si_usa_e_senza_verifica_e_esito_incerto() {
        // una sessione aperta davvero con "Sposta" non vale per una prenotazione nuova
        let sposta = |b: &str| {
            let mut s = Sessione::verso(b, CF, NRE).unwrap();
            s.elenco().unwrap();
            s.alternative(0).unwrap();
            assert_eq!(s.modo(), Modo::Sposta);
            s
        };
        let (esito, _, viste) = prova_prenota(
            |b| {
                let mut r = fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE"));
                r.extend(nessuna(b));
                r.extend(nessuna(b));
                r.extend(date_nuove(b, &[ECO]));
                r.extend([ok("selezionata"), riep_nuova(b, &[ECO], false), ok("?")]);
                for _ in 0..3 {
                    r.extend(nessuna(b));
                }
                r
            },
            Some(&sposta),
            LIBERA,
            false,
        );
        assert!(matches!(&esito, Err(Errore::Incerto(m)) if m.contains("risulta non verificabile")), "{esito:?}");
        assert_eq!(viste.len(), 26);
    }

    #[test]
    fn seconda_lettura_erogata_non_prenota() {
        let (esito, fasi, viste) = prova_prenota(
            |b| {
                let mut r = nessuna(b);
                r.extend(lista(b, &[("EROGATO", GIORNO, "OSPEDALE", "ECO 2", ECO)]));
                r
            },
            None,
            LIBERA,
            false,
        );
        assert!(matches!(&esito, Err(Errore::Inattesa(m)) if m.contains("non prenoto")), "{esito:?}");
        assert!(fasi.is_empty() && viste.len() == 4);
    }

    #[test]
    fn sposta_non_usa_la_sessione_di_una_prenotazione_nuova() {
        // la sessione di una ricerca "nuova" della stessa ricetta: "Sposta" ne apre una sua
        let (base, h) = portale(|b| {
            let mut r = date_nuove(b, &[ECO]);
            r.extend(lista(b, &prenotata(PRIMA, "MOLINETTE")));
            r.extend(fino_alle_date(b, &prenotata(PRIMA, "MOLINETTE")));
            r
        });
        let mut tenuta = Sessione::verso(&base, CF, NRE).unwrap();
        tenuta.cerca_nuova(0).unwrap();
        let nuova = |_: Option<Duration>| Sessione::verso(&base, CF, NRE);
        let z = Zona::new(Dove::Tutte);
        // prova: si ferma prima del "Seleziona" perche' la chiave non c'e', ma dopo aver aperto "Sposta" da capo
        let e = sposta(
            &nuova,
            Some(tenuta),
            "202001010900|NESSUNA",
            &z,
            &Calendario::default(),
            true,
            &attese(),
            &mut |_| Ok(()),
        )
        .unwrap_err();
        assert!(e.to_string().contains("non è più disponibile"), "{e}");
        let viste = h.join().unwrap();
        assert_eq!(viste.len(), 7 + 2 + 6);
        assert_eq!(viste[9].0, "GET /web/guest/lista-prenotazioni HTTP/1.1");
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
