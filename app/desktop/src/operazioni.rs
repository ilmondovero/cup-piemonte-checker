//! Spostare, prenotare e disdire dall'app, quando lo chiede l'utente o, con la prenotazione automatica accesa da lui,
//! dopo un controllo: una cosa alla volta per ricetta (mai durante un controllo), lo stato salvato sul disco prima
//! della Conferma (se non si salva, la Conferma non parte) e un esito incerto che blocca altre operazioni finche'
//! l'utente non ha controllato sul portale.

use std::cell::Cell;
use std::panic::{catch_unwind, AssertUnwindSafe};

use chrono::NaiveDateTime;
use cup_core::operazioni::{Fase, Prenotata, Spostamento, CALL_CENTER};
use cup_core::{contatti, Errore, Sessione};

use crate::archivio::{Dati, Operazione, Ricetta, Visita, ISO};
use crate::controlli::{adesso, blocca, impostazioni, tipo, Attivita, Notifica, Stato, TENUTA};
use crate::segnalazioni;

/// Le fasi dell'operazione, come le chiama il motore.
pub type Fasi<'a> = &'a mut dyn FnMut(Fase) -> Result<(), String>;

/// Com'e' finita un'operazione: il messaggio per l'interfaccia e, se serve, una notifica (l'utente puo' aver chiuso
/// la finestra durante la verifica).
#[derive(Debug)]
pub struct Fine {
    pub ok: bool,
    pub messaggio: String,
    pub notifica: Option<Notifica>,
}

/// Prende la ricetta per un'operazione, se si puo': niente se c'e' gia' qualcosa in corso o un esito incerto da
/// controllare. Da qui la ricetta e' occupata finche' non si chiama `Stato::libera`.
fn prendi(
    stato: &Stato,
    id: u64,
    attivita: Attivita,
    ammessa: impl FnOnce(&Ricetta) -> Result<(), String>,
) -> Result<Ricetta, String> {
    let mut in_corso = blocca(&stato.in_corso);
    if stato.in_chiusura() {
        return Err("L'app si sta aggiornando e tra poco si riapre: riprova tra un minuto.".into());
    }
    if let Some(a) = in_corso.get(&id) {
        return Err(match a {
            Attivita::Controllo => "C'è un controllo in corso su questa ricetta: riprova tra qualche minuto.",
            _ => "C'è già un'operazione in corso su questa ricetta.",
        }
        .into());
    }
    let d = stato.dati();
    let r = d.ricette.iter().find(|r| r.id == id).ok_or("Ricetta non trovata")?.clone();
    if r.incerta.is_some() {
        return Err("Prima controlla sul sito del CUP com'è andata l'operazione precedente, poi tocca \
                    «Ho controllato»."
            .into());
    }
    ammessa(&r)?;
    in_corso.insert(id, attivita);
    Ok(r)
}

/// Salva sulla ricetta la fase dell'operazione. Per la Conferma deve riuscire: se no la Conferma non parte (e su
/// disco resta lo stato di prima, che dice il vero).
fn segna(stato: &Stato, id: u64, tipo: &str, fase: Fase, descrizione: &str) -> Result<(), String> {
    let nome = match fase {
        Fase::Riepilogo | Fase::Contatti { .. } => return Ok(()),
        Fase::SenzaContatti => {
            stato.registro.scrivi(&format!("ricetta {id}: contatti rifiutati dal portale, confermo senza"));
            return Ok(());
        }
        Fase::Conferma => "conferma",
        Fase::Verifica => "verifica",
    };
    let mut d = stato.dati();
    let r = d.ricetta(id)?;
    let prima = r.operazione.replace(Operazione {
        tipo: tipo.into(),
        fase: nome.into(),
        ts: adesso(),
        descrizione: descrizione.into(),
    });
    let esito = stato.salva(&d);
    if esito.is_err() && fase == Fase::Conferma {
        d.ricetta(id)?.operazione = prima;
    }
    esito
}

/// La voce del registro per un'operazione non riuscita: il tipo, mai il testo (date, luoghi, prestazioni).
fn voce(e: &Errore) -> String {
    match e {
        Errore::Inattesa(_) => "fermato prima della conferma".into(),
        e => tipo(e),
    }
}

/// Libera la ricetta alla fine dell'operazione, anche dopo un errore imprevisto.
struct Occupata<'a>(&'a Stato, u64);

impl Drop for Occupata<'_> {
    fn drop(&mut self) {
        self.0.libera(self.1);
    }
}

/// L'esito incerto: la ricetta si ferma e niente altre operazioni finche' l'utente non controlla. Le date trovate
/// prima non valgono piu' (la prenotazione puo' essere cambiata).
fn incerta(d: &mut Dati, r: &Ricetta, cosa: &str, mut messaggio: String) -> Fine {
    if let Ok(x) = d.ricetta(r.id) {
        if x.automatica {
            // niente altri tentativi da sola finche' non si sa com'e' andata: decide l'utente
            x.automatica = false;
            messaggio
                .push_str(" La prenotazione automatica è spenta: riaccendila da «Modifica» dopo aver controllato.");
        }
        x.operazione = None;
        x.incerta = Some(messaggio.clone());
        x.attiva = false;
        dopo_incerta(x);
    }
    Fine {
        ok: false,
        notifica: Some(Notifica {
            ricetta: r.id,
            tipo: "esito incerto",
            titolo: format!("{}: {cosa} da verificare", r.nome),
            testo: messaggio.clone(),
        }),
        messaggio,
    }
}

/// Dopo un errore del motore: incerto se la Conferma era partita (il motore lo dice, oppure l'operazione salvata
/// lo mostra, dopo un errore interno), se no non e' cambiato niente.
fn fallita(stato: &Stato, d: &mut Dati, r: &Ricetta, cosa: &str, descrizione: &str, e: Errore, da_sola: bool) -> Fine {
    let partita = d.ricette.iter().any(|x| x.id == r.id && x.operazione.is_some());
    // un esito incerto si segnala allo sviluppatore (se l'utente le ha accese): una frase fissa, senza il messaggio
    let segnala = || {
        let testo = if cosa == "disdetta" { segnalazioni::INCERTA_DISDETTA } else { segnalazioni::INCERTO };
        segnalazioni::segnala(segnalazioni::Tipo::Errore, testo)
    };
    match e {
        Errore::Incerto(m) => {
            stato.registro.scrivi(&format!("ricetta {}: {cosa}, esito incerto", r.id));
            segnala();
            incerta(d, r, cosa, format!("{} {descrizione}: {m}", maiuscola(cosa)))
        }
        _ if partita => {
            stato.registro.scrivi(&format!("ricetta {}: {cosa}, errore interno dopo la conferma", r.id));
            segnala();
            let m = format!(
                "{} {descrizione}: errore dell'app dopo la conferma, l'esito non è verificato. Controlla subito sul \
                 sito del CUP (Recupera Prenotazioni) o al call center {CALL_CENTER}.",
                maiuscola(cosa)
            );
            incerta(d, r, cosa, m)
        }
        e => {
            let fatto = if cosa == "spostamento" { "non fatto" } else { "non fatta" };
            stato.registro.scrivi(&format!("ricetta {}: {cosa} {fatto} ({})", r.id, voce(&e)));
            if let Ok(x) = d.ricetta(r.id) {
                if cosa != "disdetta" {
                    // il motore puo' aver aperto gli appuntamenti con una sessione nuova: i 20 minuti ripartono da qui
                    x.ultimo_tentativo = Some(adesso());
                }
                if matches!(e, Errore::GiaPrenotata(_)) {
                    // prenotata nel frattempo (dal sito del CUP, al telefono): da ora si cercano date prima, ma non
                    // si sposta da sola una data scelta dall'utente
                    x.nuova = false;
                    x.automatica = false;
                    if let Some(es) = x.esito.as_mut() {
                        es.buone.clear();
                        es.nota =
                            Some(format!("{e}: al prossimo controllo l'app legge la prenotazione e cerca date prima."));
                    }
                }
            }
            let mut messaggio = format!("{} {descrizione} {fatto}: {e}", maiuscola(cosa));
            if !da_sola {
                return Fine { ok: false, messaggio, notifica: None };
            }
            // qualcosa di diverso da una data sparita (portale cambiato, Riepilogo diverso...) o una ricetta con piu'
            // prestazioni (ogni tentativo tiene occupate altre date): niente altri tentativi da sola
            let piu_prestazioni = r.nuova && r.esito.as_ref().is_some_and(|x| x.cosa.contains(" + "));
            let spegni = !a_vuoto(&e) || piu_prestazioni;
            if let Ok(x) = d.ricetta(r.id) {
                if spegni && x.automatica {
                    x.automatica = false;
                    stato.registro.scrivi(&format!("ricetta {}: prenotazione automatica spenta", r.id));
                    messaggio.push_str(if piu_prestazioni {
                        ". Questa ricetta ha più prestazioni: per non tenere occupate altre date a ogni tentativo ho \
                         spento la prenotazione automatica"
                    } else {
                        ". Il portale ha risposto in un modo inatteso: per sicurezza ho spento la prenotazione \
                         automatica"
                    });
                }
            }
            let restano =
                d.ricette.iter().any(|x| x.id == r.id && x.esito.as_ref().is_some_and(|e| !e.buone.is_empty()));
            messaggio.truncate(messaggio.trim_end_matches('.').len());
            messaggio.push('.');
            if restano {
                messaggio.push_str(" Le date trovate restano nell'app.");
            }
            Fine {
                ok: false,
                notifica: Some(Notifica {
                    ricetta: r.id,
                    tipo: "automatica non riuscita",
                    titolo: format!("{}: prenotazione automatica non riuscita", r.nome),
                    testo: messaggio.clone(),
                }),
                messaggio,
            }
        }
    }
}

/// Un tentativo andato a vuoto come puo' capitare sempre: la data presa da un altro nel frattempo o non piu' buona,
/// il portale lento o sovraccarico. Tutto il resto (pagine o Riepiloghi che il motore non riconosce, ricetta non
/// trovata, errori interni) spegne la prenotazione automatica: riprovare da sola terrebbe occupate altre date.
fn a_vuoto(e: &Errore) -> bool {
    match e {
        Errore::Timeout | Errore::Sovraccarico(_) => true,
        Errore::Inattesa(m) => {
            m.starts_with("La data scelta non è più disponibile") || m.starts_with("La data scelta non va più bene")
        }
        _ => false,
    }
}

/// Dopo un esito incerto le date trovate prima non valgono piu' (la prenotazione puo' essere cambiata).
fn dopo_incerta(r: &mut Ricetta) {
    if let Some(e) = r.esito.as_mut() {
        e.buone.clear();
        e.nota =
            Some("Dopo aver controllato sul sito del CUP, riprendi i controlli: l'app rilegge la prenotazione.".into());
    }
}

/// Nel registro solo se ci sono, mai i valori.
fn contatti_nel_registro(stato: &Stato, r: &Ricetta) {
    let si = |c: &Option<String>| if c.is_some() { "si" } else { "no" };
    stato.registro.scrivi(&format!(
        "ricetta {}: contatti: email {}, cellulare {}",
        r.id,
        si(&r.email),
        si(&r.cellulare)
    ));
}

/// Cosa e' successo ai contatti durante un'operazione, come lo dice il motore: quali sono andati davvero nel
/// Riepilogo (email, cellulare) e se il portale li ha rifiutati.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct Scritti {
    email: bool,
    cellulare: bool,
    rifiutati: bool,
}

impl Scritti {
    fn segna(&mut self, f: Fase) {
        match f {
            Fase::Contatti { email, cellulare } => (self.email, self.cellulare) = (email, cellulare),
            Fase::SenzaContatti => self.rifiutati = true,
            _ => {}
        }
    }
}

/// Dove il CUP manda la conferma, per il messaggio dopo una prenotazione o uno spostamento riusciti (mascherati).
/// Parla solo dei contatti che sono andati davvero nel Riepilogo.
fn dove_la_conferma(r: &Ricetta, s: Scritti) -> String {
    let email = r.email.as_deref().filter(|_| s.email);
    let cellulare = r.cellulare.as_deref().filter(|_| s.cellulare);
    if email.is_none() && cellulare.is_none() {
        return String::new();
    }
    if s.rifiutati {
        let quali = match (email, cellulare) {
            (Some(_), Some(_)) => "email e cellulare",
            (Some(_), None) => "l'email",
            _ => "il cellulare",
        };
        return format!("\nIl portale non ha accettato {quali}: la conferma arriva ai recapiti che il CUP aveva già.");
    }
    let mut dove = vec![];
    if let Some(e) = email {
        dove.push(format!("la conferma per email a {}", contatti::email_mascherata(e)));
    }
    if let Some(c) = cellulare {
        dove.push(format!("gli SMS al {}", contatti::cellulare_mascherato(c)));
    }
    format!("\nIl CUP manda {}.", dove.join(" e "))
}

fn maiuscola(s: &str) -> String {
    let mut c = s.chars();
    c.next().map_or(String::new(), |p| p.to_uppercase().chain(c).collect())
}

/// Il motore di uno spostamento: la ricetta, la sessione del controllo (se valida), le fasi.
pub type MotoreSposta<'a> = &'a dyn Fn(&Ricetta, Option<Sessione>, Fasi) -> Result<Spostamento, Errore>;

/// Sposta la ricetta sulla data con questa chiave, che deve essere tra le date buone dell'ultimo controllo. `inizio`
/// si chiama quando l'operazione parte (per mostrarla nell'interfaccia).
pub fn sposta(stato: &Stato, id: u64, chiave: &str, motore: MotoreSposta, inizio: &dyn Fn()) -> Result<Fine, String> {
    sposta_come(stato, id, chiave, motore, inizio, false)
}

/// Come `sposta`, ma deciso dall'app (prenotazione automatica): solo se e' ancora accesa e la ricetta attiva.
pub fn sposta_da_sola(stato: &Stato, id: u64, chiave: &str, motore: MotoreSposta) -> Result<Fine, String> {
    sposta_come(stato, id, chiave, motore, &|| {}, true)
}

/// La prenotazione automatica non e' partita (spenta, ricetta occupata, app in chiusura): la data torna tentabile,
/// cosi' al prossimo controllo si puo' riprovare.
pub fn non_partita(stato: &Stato, id: u64, chiave: &str) {
    let mut d = stato.dati();
    if let Ok(r) = d.ricetta(id) {
        r.tentate.retain(|t| t != chiave);
        let _ = stato.salva(&d);
    }
}

/// Per un'operazione decisa dall'app: l'utente puo' aver spento l'automatica o messo in pausa durante il controllo.
fn ancora_da_sola(r: &Ricetta, da_sola: bool) -> Result<(), String> {
    if da_sola && !(r.automatica && r.attiva) {
        return Err("Prenotazione automatica spenta nel frattempo.".into());
    }
    Ok(())
}

/// Nel titolo della notifica: chi ha deciso.
fn come(da_sola: bool) -> &'static str {
    if da_sola {
        " in automatico"
    } else {
        ""
    }
}

fn sposta_come(
    stato: &Stato,
    id: u64,
    chiave: &str,
    motore: MotoreSposta,
    inizio: &dyn Fn(),
    da_sola: bool,
) -> Result<Fine, String> {
    let mut scelta = None;
    let r = prendi(stato, id, Attivita::Spostamento, |r| {
        ancora_da_sola(r, da_sola)?;
        if r.nuova {
            return Err("Questa ricetta non è ancora prenotata: usa «Prenota qui».".into());
        }
        scelta = r.esito.as_ref().and_then(|e| e.buone.iter().find(|d| d.chiave == chiave)).cloned();
        scelta
            .as_ref()
            .map(|_| ())
            .ok_or("Questa data non è più tra quelle trovate: aspetta il prossimo controllo.".into())
    })?;
    let _occupata = Occupata(stato, id);
    let scelta = scelta.expect("controllata da prendi");
    inizio();
    // la sessione del controllo tiene la proposta: si prende (non si copia), se e' recente e della stessa ricetta
    let tenuta = blocca(&stato.tenute)
        .remove(&id)
        .filter(|t| adesso() - t.ts < TENUTA && t.impostazioni == impostazioni(&r))
        .map(|t| t.sessione);
    stato.registro.scrivi(&format!(
        "ricetta {id}: spostamento{}{}",
        come(da_sola),
        if tenuta.is_some() { " (con la sessione del controllo)" } else { "" }
    ));
    contatti_nel_registro(stato, &r);
    let descrizione = format!("al {} · {}", scelta.quando, scelta.luogo);
    let scritti = Cell::new(Scritti::default());
    let esito = catch_unwind(AssertUnwindSafe(|| {
        motore(&r, tenuta, &mut |f| {
            let mut s = scritti.get();
            s.segna(f);
            scritti.set(s);
            segna(stato, id, "spostamento", f, &descrizione)
        })
    }))
    .unwrap_or_else(|_| Err(Errore::Inattesa("errore interno dell'app".into())));
    let fine = {
        let mut d = stato.dati();
        let fine = match esito {
            Ok(s) if s.fatto => {
                let dopo = Visita::spostata(&s.dopo, &s.prima.cosa);
                if let Ok(x) = d.ricetta(id) {
                    x.operazione = None;
                    // "Sposta" e' stato aperto adesso: i 20 minuti ripartono da qui
                    x.ultimo_tentativo = Some(adesso());
                    x.tentate.clear(); // da ora contano le date prima di quella nuova
                    let e = x.esito.get_or_insert_with(Default::default);
                    e.ts = adesso();
                    e.attuale = Some(dopo.clone());
                    e.buone.clear();
                    e.errore = None;
                    e.nota = Some(format!("Prenotazione spostata dall'app{}.", come(da_sola)));
                    e.note_visita = s.note.clone();
                }
                stato.registro.scrivi(&format!("ricetta {id}: spostamento fatto"));
                let note = if s.note.is_empty() { "" } else { "\nNell'app trovi le note per la visita." };
                let conferma = dove_la_conferma(&r, scritti.get());
                Fine {
                    ok: true,
                    messaggio: format!("Prenotazione spostata: {} · {}{conferma}", dopo.quando, dopo.luogo),
                    notifica: Some(Notifica {
                        ricetta: id,
                        tipo: "spostata",
                        titolo: format!("{}: prenotazione spostata{}", r.nome, come(da_sola)),
                        testo: format!("{} · {}{note}{conferma}", dopo.quando, dopo.luogo),
                    }),
                }
            }
            Ok(_) => {
                Fine { ok: false, messaggio: "Prova arrivata al Riepilogo, niente confermato.".into(), notifica: None }
            }
            Err(e) => fallita(stato, &mut d, &r, "spostamento", &descrizione, e, da_sola),
        };
        let _ = stato.salva(&d);
        fine
    };
    Ok(fine)
}

/// Il motore di una prima prenotazione: la ricetta, la sessione del controllo (se valida), le fasi.
pub type MotorePrenota<'a> = &'a dyn Fn(&Ricetta, Option<Sessione>, Fasi) -> Result<Prenotata, Errore>;

/// Prenota una ricetta mai prenotata sulla data con questa chiave, che deve essere tra le date buone dell'ultimo
/// controllo. Come `sposta`: lo stato salvato prima della Conferma, un esito incerto ferma la ricetta.
pub fn prenota(stato: &Stato, id: u64, chiave: &str, motore: MotorePrenota, inizio: &dyn Fn()) -> Result<Fine, String> {
    prenota_come(stato, id, chiave, motore, inizio, false)
}

/// Come `prenota`, ma deciso dall'app (prenotazione automatica).
pub fn prenota_da_sola(stato: &Stato, id: u64, chiave: &str, motore: MotorePrenota) -> Result<Fine, String> {
    prenota_come(stato, id, chiave, motore, &|| {}, true)
}

fn prenota_come(
    stato: &Stato,
    id: u64,
    chiave: &str,
    motore: MotorePrenota,
    inizio: &dyn Fn(),
    da_sola: bool,
) -> Result<Fine, String> {
    let mut scelta = None;
    let r = prendi(stato, id, Attivita::Prenotazione, |r| {
        ancora_da_sola(r, da_sola)?;
        if !r.nuova {
            return Err("Questa ricetta risulta già prenotata: l'app cerca date prima, con \"Sposta qui\".".into());
        }
        scelta = r.esito.as_ref().and_then(|e| e.buone.iter().find(|d| d.chiave == chiave)).cloned();
        scelta
            .as_ref()
            .map(|_| ())
            .ok_or("Questa data non è più tra quelle trovate: aspetta il prossimo controllo.".into())
    })?;
    let _occupata = Occupata(stato, id);
    let scelta = scelta.expect("controllata da prendi");
    inizio();
    let tenuta = blocca(&stato.tenute)
        .remove(&id)
        .filter(|t| adesso() - t.ts < TENUTA && t.impostazioni == impostazioni(&r))
        .map(|t| t.sessione);
    stato.registro.scrivi(&format!(
        "ricetta {id}: prenotazione{}{}",
        come(da_sola),
        if tenuta.is_some() { " (con la sessione del controllo)" } else { "" }
    ));
    contatti_nel_registro(stato, &r);
    let descrizione = format!("al {} · {}", scelta.quando, scelta.luogo);
    let scritti = Cell::new(Scritti::default());
    let esito = catch_unwind(AssertUnwindSafe(|| {
        motore(&r, tenuta, &mut |f| {
            let mut s = scritti.get();
            s.segna(f);
            scritti.set(s);
            segna(stato, id, "prenotazione", f, &descrizione)
        })
    }))
    .unwrap_or_else(|_| Err(Errore::Inattesa("errore interno dell'app".into())));
    let fine = {
        let mut d = stato.dati();
        let fine = match esito {
            Ok(p) if p.fatto => {
                let dopo = Visita::spostata(&p.dopo, &p.prestazioni.join(" + "));
                if let Ok(x) = d.ricetta(id) {
                    x.operazione = None;
                    // da ora e' una ricetta prenotata: i controlli cercano date prima, con "Sposta"
                    x.nuova = false;
                    // gli appuntamenti sono stati aperti adesso: i 20 minuti ripartono da qui
                    x.ultimo_tentativo = Some(adesso());
                    x.notificate.clear();
                    x.tentate.clear();
                    let e = x.esito.get_or_insert_with(Default::default);
                    e.ts = adesso();
                    e.attuale = Some(dopo.clone());
                    e.buone.clear();
                    e.errore = None;
                    e.zona.clear(); // "della prenotazione" adesso ha un senso: si rilegge al prossimo controllo
                    e.solo_proposta = false;
                    e.nota = Some(format!("Prenotazione fatta dall'app{}. Da ora cerca date prima.", come(da_sola)));
                    e.note_visita = p.note.clone();
                }
                stato.registro.scrivi(&format!("ricetta {id}: prenotazione fatta"));
                let note = if p.note.is_empty() { "" } else { "\nNell'app trovi le note per la visita." };
                let conferma = dove_la_conferma(&r, scritti.get());
                Fine {
                    ok: true,
                    messaggio: format!("Prenotazione fatta: {} · {}{conferma}", dopo.quando, dopo.luogo),
                    notifica: Some(Notifica {
                        ricetta: id,
                        tipo: "prenotata",
                        titolo: format!("{}: prenotazione fatta{}", r.nome, come(da_sola)),
                        testo: format!("{} · {}{note}{conferma}", dopo.quando, dopo.luogo),
                    }),
                }
            }
            Ok(_) => {
                Fine { ok: false, messaggio: "Prova arrivata al Riepilogo, niente confermato.".into(), notifica: None }
            }
            Err(e) => fallita(stato, &mut d, &r, "prenotazione", &descrizione, e, da_sola),
        };
        let _ = stato.salva(&d);
        fine
    };
    Ok(fine)
}

/// Il motore di una disdetta: la ricetta, data e ora, la chiave del luogo, le fasi.
pub type MotoreDisdici<'a> = &'a dyn Fn(&Ricetta, NaiveDateTime, Option<&str>, Fasi) -> Result<bool, Errore>;

/// Disdice la prenotazione che l'app mostra per la ricetta (quella letta dall'ultimo controllo).
pub fn disdici(stato: &Stato, id: u64, motore: MotoreDisdici, inizio: &dyn Fn()) -> Result<Fine, String> {
    let mut visita = None;
    let r = prendi(stato, id, Attivita::Disdetta, |r| {
        visita = r.esito.as_ref().and_then(|e| e.attuale.clone()).filter(|v| !v.iso.is_empty());
        visita.as_ref().map(|_| ()).ok_or(
            "L'app deve rileggere la prenotazione prima di disdirla: riprendi i controlli e aspetta il prossimo."
                .into(),
        )
    })?;
    let _occupata = Occupata(stato, id);
    let v = visita.expect("controllata da prendi");
    let quando = NaiveDateTime::parse_from_str(&v.iso, ISO)
        .map_err(|_| "Data della prenotazione non leggibile: non disdico".to_string())?;
    inizio();
    stato.registro.scrivi(&format!("ricetta {id}: disdetta"));
    let descrizione = format!("del {} · {}", v.quando, v.luogo);
    let luogo = Some(v.chiave_luogo.as_str()).filter(|l| !l.is_empty());
    let esito = catch_unwind(AssertUnwindSafe(|| {
        motore(&r, quando, luogo, &mut |f| segna(stato, id, "disdetta", f, &descrizione))
    }))
    .unwrap_or_else(|_| Err(Errore::Inattesa("errore interno dell'app".into())));
    let fine = {
        let mut d = stato.dati();
        let fine = match esito {
            Ok(true) => {
                if let Ok(x) = d.ricetta(id) {
                    x.operazione = None;
                    x.attiva = false; // non c'e' piu' niente da anticipare
                    x.automatica = false; // e niente prenotazione da sola al posto di quella disdetta
                    let e = x.esito.get_or_insert_with(Default::default);
                    e.ts = adesso();
                    e.attuale = None;
                    e.buone.clear();
                    e.errore = None;
                    e.note_visita.clear();
                    e.nota = Some(format!("Prenotazione {descrizione} disdetta dall'app."));
                }
                blocca(&stato.tenute).remove(&id);
                stato.registro.scrivi(&format!("ricetta {id}: disdetta fatta"));
                Fine {
                    ok: true,
                    messaggio: format!("Prenotazione disdetta: {} · {}", v.quando, v.luogo),
                    notifica: Some(Notifica {
                        ricetta: id,
                        tipo: "disdetta",
                        titolo: format!("{}: prenotazione disdetta", r.nome),
                        testo: format!("{} · {}", v.quando, v.luogo),
                    }),
                }
            }
            Ok(false) => {
                Fine { ok: false, messaggio: "Prova arrivata al dialogo, niente disdetto.".into(), notifica: None }
            }
            Err(e) => fallita(stato, &mut d, &r, "disdetta", &descrizione, e, false),
        };
        let _ = stato.salva(&d);
        fine
    };
    Ok(fine)
}

/// All'avvio: un'operazione rimasta salvata vuol dire che l'app si e' fermata dopo la Conferma. L'esito e' incerto.
/// Ritorna una notifica per ogni ricetta, con data, ora e luogo dell'operazione.
pub fn riprendi(d: &mut Dati) -> Vec<Notifica> {
    let mut notifiche = vec![];
    for r in d.ricette.iter_mut() {
        if let Some(op) = r.operazione.take() {
            let (cosa, breve) = match op.tipo.as_str() {
                "disdetta" => ("la disdetta", "disdetta"),
                "prenotazione" => ("la prenotazione", "prenotazione"),
                _ => ("lo spostamento", "spostamento"),
            };
            let messaggio = format!(
                "L'app si è chiusa durante {cosa} {}, dopo la conferma: l'esito non è verificato. Controlla sul sito \
                 del CUP (Recupera Prenotazioni) o al call center {CALL_CENTER}.",
                op.descrizione
            );
            let mut messaggio = messaggio;
            if std::mem::take(&mut r.automatica) {
                messaggio
                    .push_str(" La prenotazione automatica è spenta: riaccendila da «Modifica» dopo aver controllato.");
            }
            r.incerta = Some(messaggio.clone());
            r.attiva = false;
            dopo_incerta(r);
            notifiche.push(Notifica {
                ricetta: r.id,
                tipo: "operazione interrotta",
                titolo: format!("{}: {breve} da verificare", r.nome),
                testo: messaggio,
            });
        }
    }
    notifiche
}

#[cfg(test)]
mod test {
    use std::path::PathBuf;

    use chacha20poly1305::aead::{KeyInit, OsRng};
    use chacha20poly1305::XChaCha20Poly1305;
    use cup_core::{Luogo, Prenotazione, Slot};

    use super::*;
    use crate::archivio::{Archivio, Data, Esito};
    use crate::controlli::Tenuta;
    use crate::registro::Registro;

    const CHIAVE: &str = "202612010900|SANGIOVANNIBOSCOAMB";

    fn quando(g: u32, m: u32, a: i32) -> NaiveDateTime {
        chrono::NaiveDate::from_ymd_opt(a, m, g).unwrap().and_hms_opt(9, 0, 0).unwrap()
    }

    fn luogo(sede: &str) -> Luogo {
        Luogo { sede: sede.into(), ambulatorio: "AMB".into(), indirizzo: "- TORINO (TO)".into() }
    }

    fn prima() -> Prenotazione {
        Prenotazione { quando: quando(7, 1, 2027), luogo: luogo("MOLINETTE"), cosa: "VISITA".into() }
    }

    fn ricetta() -> Ricetta {
        Ricetta {
            id: 1,
            nome: "Genetica".into(),
            cf: "RSSMRA80A01L219X".into(),
            nre: "010A00000000001".into(),
            dove: "tutte".into(),
            valore: String::new(),
            evita: vec![],
            attiva: true,
            ultimo_tentativo: Some(0),
            esito: Some(Esito {
                ts: 1,
                controllo: true,
                attuale: Some(Visita::da(&prima())),
                buone: vec![Data {
                    chiave: CHIAVE.into(),
                    quando: "mar 01/12/2026 ore 09:00".into(),
                    luogo: "SAN GIOVANNI BOSCO, AMB, TORINO (TO)".into(),
                    proposta: false,
                }],
                date: 1,
                ..Default::default()
            }),
            notificate: vec![],
            assenze: 0,
            operazione: None,
            incerta: None,
            nuova: false,
            email: None,
            cellulare: None,
            automatica: false,
            tentate: vec![],
        }
    }

    fn stato(nome: &str) -> (Stato, PathBuf) {
        let c = std::env::temp_dir().join(format!("cup-test-op-{nome}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&c);
        std::fs::create_dir_all(&c).unwrap();
        let archivio = Archivio::con_chiave(c.join("ricette.cifrate"), XChaCha20Poly1305::generate_key(&mut OsRng));
        let dati = Dati { prossimo_id: 9, ricette: vec![ricetta()], eliminate: vec![] };
        (Stato::new(archivio, Registro::new(&c), dati), c)
    }

    fn spostata(note: Vec<String>) -> Spostamento {
        Spostamento {
            prima: prima(),
            dopo: Slot {
                quando: quando(1, 12, 2026),
                luogo: luogo("SAN GIOVANNI BOSCO"),
                seleziona: None,
                proposta: false,
            },
            note,
            fatto: true,
        }
    }

    /// Nel registro non finiscono mai date, luoghi o nomi.
    fn registro_pulito(s: &Stato) {
        for riga in s.registro.ultime(50) {
            let dopo_ora = &riga[19..];
            assert!(!dopo_ora.contains("2026") && !dopo_ora.contains("2027"), "{riga}");
            assert!(!riga.contains("MOLINETTE") && !riga.contains("GIOVANNI") && !riga.contains("Genetica"), "{riga}");
        }
    }

    #[test]
    fn spostamento_con_lo_stato_salvato_prima_della_conferma() {
        let (s, c) = stato("sposta");
        let iniziato = Cell::new(false);
        let motore = |r: &Ricetta, tenuta: Option<Sessione>, fase: Fasi| {
            assert!(tenuta.is_none() && r.id == 1);
            assert!(s.in_corso().get(&1) == Some(&Attivita::Spostamento));
            fase(Fase::Riepilogo).unwrap();
            fase(Fase::Conferma).unwrap();
            // sul disco, prima della Conferma: l'operazione in corso
            let op = s.archivio.leggi().unwrap().ricette[0].operazione.clone().unwrap();
            assert_eq!((op.tipo.as_str(), op.fase.as_str()), ("spostamento", "conferma"));
            assert!(op.descrizione.contains("01/12/2026 ore 09:00"));
            fase(Fase::Verifica).unwrap();
            Ok(spostata(vec!["Presentarsi a digiuno".into()]))
        };
        let f = sposta(&s, 1, CHIAVE, &motore, &|| iniziato.set(true)).unwrap();
        assert!(f.ok && iniziato.get());
        assert_eq!(
            f.messaggio,
            "Prenotazione spostata: mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO, AMB, TORINO (TO)"
        );
        let n = f.notifica.unwrap();
        assert_eq!(n.titolo, "Genetica: prenotazione spostata");
        assert!(n.testo.starts_with("mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO") && n.testo.contains("note"));
        let d = s.archivio.leggi().unwrap();
        let r = &d.ricette[0];
        let e = r.esito.as_ref().unwrap();
        assert!(r.operazione.is_none() && r.attiva && e.buone.is_empty());
        assert_eq!(e.attuale.as_ref().unwrap().iso, "2026-12-01T09:00");
        assert_eq!(e.attuale.as_ref().unwrap().cosa, "VISITA");
        assert_eq!(e.note_visita, ["Presentarsi a digiuno"]);
        assert!(r.ultimo_tentativo.unwrap() > 0);
        assert!(s.in_corso().is_empty());
        registro_pulito(&s);
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn prenotazione_automatica_riuscita_spenta_o_fermata() {
        let accesa = |nome: &str| {
            let (s, c) = stato(nome);
            let mut d = s.dati();
            d.ricette[0].automatica = true;
            d.ricette[0].tentate = vec![CHIAVE.into()];
            drop(d);
            (s, c)
        };
        let mai = |_: &Ricetta, _: Option<Sessione>, _: Fasi| -> Result<Spostamento, Errore> { panic!("no") };
        // riuscita: la notifica dice che l'ha fatto l'app, e le date tentate ricominciano dalla data nuova
        let (s, c) = accesa("auto-ok");
        let ok = |_: &Ricetta, _: Option<Sessione>, _: Fasi| Ok(spostata(vec![]));
        let f = sposta_da_sola(&s, 1, CHIAVE, &ok).unwrap();
        assert!(f.ok);
        let n = f.notifica.unwrap();
        assert_eq!(n.titolo, "Genetica: prenotazione spostata in automatico");
        assert!(n.testo.starts_with("mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO"));
        let r = &s.archivio.leggi().unwrap().ricette[0];
        assert!(r.automatica && r.tentate.is_empty());
        assert!(s.registro.ultime(5).iter().any(|x| x.ends_with("ricetta 1: spostamento in automatico")));
        registro_pulito(&s);
        std::fs::remove_dir_all(&c).unwrap();
        // spenta o in pausa nel frattempo: non parte
        let (s, c) = accesa("auto-spenta");
        s.dati().ricette[0].automatica = false;
        assert!(sposta_da_sola(&s, 1, CHIAVE, &mai).unwrap_err().contains("spenta"));
        s.dati().ricette[0].automatica = true;
        s.dati().ricette[0].attiva = false;
        assert!(sposta_da_sola(&s, 1, CHIAVE, &mai).unwrap_err().contains("spenta"));
        assert!(s.in_corso().is_empty());
        std::fs::remove_dir_all(&c).unwrap();
        // una pagina che il motore non conosce: niente confermato, automatica spenta e una notifica
        let (s, c) = accesa("auto-inattesa");
        let strana = |_: &Ricetta, _: Option<Sessione>, _: Fasi| -> Result<Spostamento, Errore> {
            Err(Errore::Inattesa("Pagina del portale non riconosciuta: sezioni [Nuova], moduli [], pulsanti []".into()))
        };
        let f = sposta_da_sola(&s, 1, CHIAVE, &strana).unwrap();
        assert!(!f.ok);
        let n = f.notifica.unwrap();
        assert_eq!(n.titolo, "Genetica: prenotazione automatica non riuscita");
        assert!(
            n.testo.contains("ho spento la prenotazione automatica") && n.testo.contains("01/12/2026"),
            "{}",
            n.testo
        );
        let r = &s.archivio.leggi().unwrap().ricette[0];
        assert!(!r.automatica && r.attiva && r.incerta.is_none());
        registro_pulito(&s);
        std::fs::remove_dir_all(&c).unwrap();
        // data presa da un altro nel frattempo, o portale lento: resta accesa, si riprova al prossimo controllo
        for (nome, e) in [
            ("auto-presa", Errore::Inattesa("La data scelta non è più disponibile.".into())),
            ("auto-timeout", Errore::Timeout),
        ] {
            let (s, c) = accesa(nome);
            let e = Cell::new(Some(e));
            let no =
                |_: &Ricetta, _: Option<Sessione>, _: Fasi| -> Result<Spostamento, Errore> { Err(e.take().unwrap()) };
            let f = sposta_da_sola(&s, 1, CHIAVE, &no).unwrap();
            let n = f.notifica.unwrap();
            assert!(!f.ok && s.dati().ricette[0].automatica, "{nome}");
            assert!(!n.testo.contains("spento") && !n.testo.contains(".."), "{}", n.testo);
            std::fs::remove_dir_all(&c).unwrap();
        }
        // esito incerto: ricetta ferma e automatica spenta, anche se l'operazione l'aveva chiesta l'utente
        let (s, c) = accesa("auto-incerta");
        let incerto = |_: &Ricetta, _: Option<Sessione>, fase: Fasi| -> Result<Spostamento, Errore> {
            fase(Fase::Conferma).unwrap();
            Err(Errore::Incerto("verifica non riuscita".into()))
        };
        let f = sposta(&s, 1, CHIAVE, &incerto, &|| {}).unwrap();
        assert!(!f.ok && f.messaggio.contains("prenotazione automatica è spenta"));
        let r = &s.archivio.leggi().unwrap().ricette[0];
        assert!(!r.automatica && !r.attiva && r.incerta.is_some());
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn contatti_al_motore_e_nel_messaggio_mai_nel_registro() {
        let con_contatti = |nome: &str| {
            let (s, c) = stato(nome);
            let mut d = s.dati();
            (d.ricette[0].email, d.ricette[0].cellulare) =
                (Some("mario.rossi@example.it".into()), Some("3471234567".into()));
            drop(d);
            (s, c)
        };
        let (s, c) = con_contatti("contatti");
        let motore = |r: &Ricetta, _: Option<Sessione>, fase: Fasi| {
            assert_eq!(r.contatti().cellulare.as_deref(), Some("3471234567"));
            fase(Fase::Contatti { email: true, cellulare: true }).unwrap();
            fase(Fase::Conferma).unwrap();
            fase(Fase::Verifica).unwrap();
            Ok(spostata(vec![]))
        };
        let f = sposta(&s, 1, CHIAVE, &motore, &|| {}).unwrap();
        let dove = "Il CUP manda la conferma per email a m***@example.it e gli SMS al *** *** 4567.";
        assert!(f.ok && f.messaggio.ends_with(dove) && f.notifica.unwrap().testo.ends_with(dove), "{}", f.messaggio);
        // il portale li rifiuta: si conferma senza, e il messaggio lo dice
        let (s2, c2) = con_contatti("contatti-rifiutati");
        // solo l'email ha un campo nel Riepilogo: il messaggio parla solo dell'email
        let rifiutati = |_: &Ricetta, _: Option<Sessione>, fase: Fasi| {
            fase(Fase::Contatti { email: true, cellulare: false }).unwrap();
            fase(Fase::Conferma).unwrap();
            fase(Fase::SenzaContatti).unwrap();
            fase(Fase::Verifica).unwrap();
            Ok(spostata(vec![]))
        };
        let f = sposta(&s2, 1, CHIAVE, &rifiutati, &|| {}).unwrap();
        assert!(f.ok && f.messaggio.contains("non ha accettato l'email:"), "{}", f.messaggio);
        assert!(!f.messaggio.contains("cellulare"), "{}", f.messaggio);
        for s in [&s, &s2] {
            let righe = s.registro.ultime(20);
            assert!(righe.iter().any(|r| r.ends_with("ricetta 1: contatti: email si, cellulare si")), "{righe:?}");
            for r in &righe {
                assert!(!r.contains("mario") && !r.contains("3471234567") && !r.contains("4567"), "{r}");
            }
        }
        assert!(s2.registro.ultime(20).iter().any(|r| r.ends_with("contatti rifiutati dal portale, confermo senza")));
        // senza contatti il messaggio resta quello di sempre
        let (s3, c3) = stato("senza-contatti");
        let f = sposta(&s3, 1, CHIAVE, &motore_senza(), &|| {}).unwrap();
        assert!(f.messaggio.ends_with("SAN GIOVANNI BOSCO, AMB, TORINO (TO)"));
        assert!(s3.registro.ultime(20).iter().any(|r| r.ends_with("contatti: email no, cellulare no")));
        for c in [c, c2, c3] {
            std::fs::remove_dir_all(&c).unwrap();
        }
    }

    fn motore_senza() -> impl Fn(&Ricetta, Option<Sessione>, Fasi) -> Result<Spostamento, Errore> {
        |_, _, fase| {
            fase(Fase::Conferma).unwrap();
            Ok(spostata(vec![]))
        }
    }

    #[test]
    fn niente_spostamento_su_date_non_trovate_o_durante_un_controllo() {
        let (s, c) = stato("rifiuti");
        let mai =
            |_: &Ricetta, _: Option<Sessione>, _: Fasi| -> Result<Spostamento, Errore> { panic!("non deve partire") };
        assert!(sposta(&s, 1, "202612020900|ALTRO", &mai, &|| {}).unwrap_err().contains("non è più tra quelle"));
        blocca(&s.in_corso).insert(1, Attivita::Controllo);
        assert!(sposta(&s, 1, CHIAVE, &mai, &|| {}).unwrap_err().contains("controllo in corso"));
        // il controllo non viene liberato da un rifiuto
        assert_eq!(s.in_corso().get(&1), Some(&Attivita::Controllo));
        // mentre l'app si chiude per un aggiornamento non parte nessuna operazione
        s.libera(1);
        assert!(s.chiudi_per_aggiornare());
        assert!(sposta(&s, 1, CHIAVE, &mai, &|| {}).unwrap_err().contains("si sta aggiornando"));
        s.riapri();
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn la_sessione_del_controllo_si_usa_solo_se_recente_e_della_stessa_ricetta() {
        let (s, c) = stato("tenuta");
        let r = ricetta();
        let tieni = |ts: i64, imp| {
            let sessione = Sessione::nuova(&r.cf, &r.nre).unwrap();
            blocca(&s.tenute).insert(1, Tenuta { sessione, ts, impostazioni: imp });
        };
        let ricevuta = Cell::new(None);
        let motore = |_: &Ricetta, t: Option<Sessione>, _: Fasi| {
            ricevuta.set(Some(t.is_some()));
            Err(Errore::Inattesa("La data scelta non è più disponibile".into()))
        };
        tieni(adesso(), impostazioni(&r));
        let f = sposta(&s, 1, CHIAVE, &motore, &|| {}).unwrap();
        assert!(!f.ok && f.notifica.is_none() && ricevuta.get() == Some(true));
        assert!(blocca(&s.tenute).is_empty(), "presa, non copiata");
        // troppo vecchia, o di quando la zona era un'altra: non si usa
        tieni(adesso() - TENUTA, impostazioni(&r));
        sposta(&s, 1, CHIAVE, &motore, &|| {}).unwrap();
        assert_eq!(ricevuta.get(), Some(false));
        let mut altra = r.clone();
        altra.dove = "comune".into();
        tieni(adesso(), impostazioni(&altra));
        sposta(&s, 1, CHIAVE, &motore, &|| {}).unwrap();
        assert_eq!(ricevuta.get(), Some(false));
        // fermato prima della Conferma: niente cambia, e nel registro solo il tipo
        let r = &s.dati().ricette[0];
        assert!(r.incerta.is_none() && r.attiva && r.esito.as_ref().unwrap().buone.len() == 1);
        assert!(s.registro.ultime(1)[0].ends_with("ricetta 1: spostamento non fatto (fermato prima della conferma)"));
        assert!(
            f.messaggio.starts_with("Spostamento al mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO"),
            "{}",
            f.messaggio
        );
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn esito_incerto_ferma_la_ricetta_e_le_altre_operazioni() {
        let (s, c) = stato("incerto");
        let motore = |_: &Ricetta, _: Option<Sessione>, fase: Fasi| -> Result<Spostamento, Errore> {
            fase(Fase::Conferma).unwrap();
            fase(Fase::Verifica).unwrap();
            Err(Errore::Incerto("Conferma inviata, esito incerto: la prenotazione risulta non verificabile.".into()))
        };
        let f = sposta(&s, 1, CHIAVE, &motore, &|| {}).unwrap();
        assert!(!f.ok);
        assert!(f.messaggio.starts_with("Spostamento al mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO"));
        assert_eq!(f.notifica.unwrap().titolo, "Genetica: spostamento da verificare");
        let d = s.archivio.leggi().unwrap();
        assert!(d.ricette[0].incerta.is_some() && !d.ricette[0].attiva && d.ricette[0].operazione.is_none());
        // finche' l'utente non ha controllato, niente altre operazioni
        let mai = |_: &Ricetta, _: Option<Sessione>, _: Fasi| -> Result<Spostamento, Errore> { panic!("no") };
        assert!(sposta(&s, 1, CHIAVE, &mai, &|| {}).unwrap_err().contains("Ho controllato"));
        registro_pulito(&s);
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn senza_salvataggio_la_conferma_non_parte_e_un_panico_dopo_la_conferma_e_incerto() {
        let (s, c) = stato("salva");
        let motore = |_: &Ricetta, _: Option<Sessione>, fase: Fasi| -> Result<Spostamento, Errore> {
            std::fs::create_dir(c.join("ricette.cifrate")).unwrap(); // il disco non accetta piu' scritture
            assert!(fase(Fase::Conferma).is_err());
            Err(Errore::Inattesa("Non riesco a salvare lo stato dello spostamento: non confermo".into()))
        };
        let f = sposta(&s, 1, CHIAVE, &motore, &|| {}).unwrap();
        assert!(!f.ok && f.notifica.is_none());
        assert!(s.dati().ricette[0].operazione.is_none() && s.dati().ricette[0].incerta.is_none());
        std::fs::remove_dir(c.join("ricette.cifrate")).unwrap();
        // un errore interno dopo la Conferma salvata: esito incerto, mai "non fatto"
        let rotto = |_: &Ricetta, _: Option<Sessione>, fase: Fasi| -> Result<Spostamento, Errore> {
            fase(Fase::Conferma).unwrap();
            panic!("indice fuori")
        };
        let f = sposta(&s, 1, CHIAVE, &rotto, &|| {}).unwrap();
        assert!(!f.ok && f.messaggio.contains("l'esito non è verificato") && f.notifica.is_some());
        assert!(s.dati().ricette[0].incerta.is_some() && s.in_corso().is_empty());
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn disdetta_ferma_i_controlli() {
        let (s, c) = stato("disdici");
        let motore = |_: &Ricetta, q: NaiveDateTime, l: Option<&str>, fase: Fasi| {
            assert_eq!((q, l), (quando(7, 1, 2027), Some("MOLINETTEAMB")));
            fase(Fase::Conferma).unwrap();
            assert_eq!(s.archivio.leggi().unwrap().ricette[0].operazione.as_ref().unwrap().tipo, "disdetta");
            fase(Fase::Verifica).unwrap();
            Ok(true)
        };
        let f = disdici(&s, 1, &motore, &|| {}).unwrap();
        assert!(f.ok);
        assert_eq!(f.notifica.unwrap().testo, "gio 07/01/2027 ore 09:00 · MOLINETTE, AMB, TORINO (TO)");
        let r = s.archivio.leggi().unwrap().ricette[0].clone();
        let e = r.esito.unwrap();
        assert!(!r.attiva && e.attuale.is_none() && e.buone.is_empty() && r.operazione.is_none());
        assert!(e.nota.unwrap().contains("disdetta"));
        // non c'e' piu' una prenotazione da disdire
        let mai = |_: &Ricetta, _: NaiveDateTime, _: Option<&str>, _: Fasi| -> Result<bool, Errore> { panic!("no") };
        assert!(disdici(&s, 1, &mai, &|| {}).unwrap_err().contains("rileggere"));
        registro_pulito(&s);
        std::fs::remove_dir_all(&c).unwrap();
    }

    /// Una ricetta mai prenotata con una data buona trovata dall'ultimo controllo.
    fn stato_nuova(nome: &str) -> (Stato, PathBuf) {
        let (s, c) = stato(nome);
        {
            let mut d = s.dati();
            let r = &mut d.ricette[0];
            r.nuova = true;
            let e = r.esito.as_mut().unwrap();
            e.attuale = None;
            e.cosa = "ECOGRAFIA".into();
        }
        (s, c)
    }

    fn prenotata() -> Prenotata {
        Prenotata {
            dopo: Slot {
                quando: quando(1, 12, 2026),
                luogo: luogo("SAN GIOVANNI BOSCO"),
                seleziona: Some("s".into()),
                proposta: false,
            },
            prestazioni: vec!["ECOGRAFIA".into()],
            note: vec![],
            fatto: true,
        }
    }

    #[test]
    fn prima_prenotazione_poi_si_cercano_date_prima() {
        let (s, c) = stato_nuova("prenota");
        let motore = |r: &Ricetta, _: Option<Sessione>, fase: Fasi| {
            assert!(r.nuova && s.in_corso().get(&1) == Some(&Attivita::Prenotazione));
            fase(Fase::Conferma).unwrap();
            let op = s.archivio.leggi().unwrap().ricette[0].operazione.clone().unwrap();
            assert_eq!((op.tipo.as_str(), op.fase.as_str()), ("prenotazione", "conferma"));
            fase(Fase::Verifica).unwrap();
            Ok(prenotata())
        };
        let f = prenota(&s, 1, CHIAVE, &motore, &|| {}).unwrap();
        assert!(f.ok);
        assert_eq!(f.messaggio, "Prenotazione fatta: mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO, AMB, TORINO (TO)");
        let n = f.notifica.unwrap();
        assert_eq!(
            (n.titolo.as_str(), n.testo.as_str()),
            ("Genetica: prenotazione fatta", "mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO, AMB, TORINO (TO)")
        );
        let r = s.archivio.leggi().unwrap().ricette[0].clone();
        let e = r.esito.unwrap();
        assert!(!r.nuova && r.attiva && r.operazione.is_none() && e.buone.is_empty());
        assert_eq!(
            e.attuale.as_ref().map(|a| (a.iso.as_str(), a.cosa.as_str())),
            Some(("2026-12-01T09:00", "ECOGRAFIA"))
        );
        assert!(e.nota.unwrap().contains("Prenotazione fatta"));
        registro_pulito(&s);
        // gia' prenotata: "Prenota qui" non vale piu', si sposta
        let mai = |_: &Ricetta, _: Option<Sessione>, _: Fasi| -> Result<Prenotata, Errore> { panic!("no") };
        assert!(prenota(&s, 1, CHIAVE, &mai, &|| {}).unwrap_err().contains("già prenotata"));
        // e al contrario: una ricetta mai prenotata non si "sposta"
        s.dati().ricette[0].nuova = true;
        let mai = |_: &Ricetta, _: Option<Sessione>, _: Fasi| -> Result<Spostamento, Errore> { panic!("no") };
        assert!(sposta(&s, 1, CHIAVE, &mai, &|| {}).unwrap_err().contains("Prenota qui"));
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn prenotata_nel_frattempo_o_esito_incerto() {
        let (s, c) = stato_nuova("prenota-gia");
        let gia = |_: &Ricetta, _: Option<Sessione>, _: Fasi| -> Result<Prenotata, Errore> {
            Err(Errore::GiaPrenotata("Nel frattempo la ricetta risulta prenotata al gio 07/01/2027 ore 09:00".into()))
        };
        let f = prenota(&s, 1, CHIAVE, &gia, &|| {}).unwrap();
        assert!(!f.ok && f.notifica.is_none() && f.messaggio.starts_with("Prenotazione al mar 01/12/2026"));
        let r = s.dati().ricette[0].clone();
        assert!(!r.nuova && r.incerta.is_none() && r.esito.unwrap().buone.is_empty());
        assert!(s.registro.ultime(1)[0].ends_with("prenotazione non fatta (la ricetta risulta già prenotata)"));
        // dopo la Conferma senza verifica: incerta, come uno spostamento
        let (s, c2) = stato_nuova("prenota-incerta");
        let incerto = |_: &Ricetta, _: Option<Sessione>, fase: Fasi| -> Result<Prenotata, Errore> {
            fase(Fase::Conferma).unwrap();
            Err(Errore::Incerto("Conferma inviata, esito incerto".into()))
        };
        let f = prenota(&s, 1, CHIAVE, &incerto, &|| {}).unwrap();
        assert_eq!(f.notifica.unwrap().titolo, "Genetica: prenotazione da verificare");
        assert!(s.dati().ricette[0].incerta.is_some() && !s.dati().ricette[0].attiva);
        registro_pulito(&s);
        std::fs::remove_dir_all(&c).unwrap();
        std::fs::remove_dir_all(&c2).unwrap();
    }

    #[test]
    fn al_riavvio_un_operazione_a_meta_e_incerta() {
        let mut d = Dati { prossimo_id: 2, ricette: vec![ricetta(), ricetta()], eliminate: vec![] };
        d.ricette[1].id = 2;
        d.ricette[1].operazione = Some(Operazione {
            tipo: "spostamento".into(),
            fase: "verifica".into(),
            ts: 5,
            descrizione: "al mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO".into(),
        });
        let n = riprendi(&mut d);
        assert_eq!(n.len(), 1);
        assert_eq!((n[0].ricetta, n[0].titolo.as_str()), (2, "Genetica: spostamento da verificare"));
        assert!(n[0].testo.contains("al mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO"));
        let r = &d.ricette[1];
        assert!(r.operazione.is_none() && !r.attiva);
        assert!(r.incerta.as_ref().unwrap().contains("durante lo spostamento al mar 01/12/2026 ore 09:00"));
        assert!(d.ricette[0].incerta.is_none() && d.ricette[0].attiva);
        // con la prenotazione automatica accesa: si spegne, e il messaggio lo dice
        d.ricette[0].automatica = true;
        d.ricette[0].operazione = d.ricette[1].incerta.as_ref().map(|_| Operazione {
            tipo: "prenotazione".into(),
            fase: "conferma".into(),
            ts: 6,
            descrizione: "al mar 01/12/2026 ore 09:00 · SAN GIOVANNI BOSCO".into(),
        });
        let n = riprendi(&mut d);
        assert!(!d.ricette[0].automatica && n[0].testo.contains("La prenotazione automatica è spenta"));
    }

    #[test]
    fn la_data_di_un_tentativo_mai_partito_torna_tentabile() {
        let (s, c) = stato("non-partita");
        s.dati().ricette[0].tentate = vec!["altra".into(), CHIAVE.into()];
        non_partita(&s, 1, CHIAVE);
        assert_eq!(s.archivio.leggi().unwrap().ricette[0].tentate, ["altra"]);
        std::fs::remove_dir_all(&c).unwrap();
    }
}
