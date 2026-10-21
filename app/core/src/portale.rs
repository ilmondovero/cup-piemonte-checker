//! La sessione sul portale: cookie, moduli JSF/ICEfaces e le richieste come le manda il browser.
//!
//! Flusso (lo stesso del bot Telegram, replicato campo per campo dai POST del browser):
//!   1. Recupera Prenotazioni: ricerca per codice fiscale + NRE -> appuntamento attuale
//!   2. "Sposta appuntamento" -> pagina Appuntamenti Proposti
//!   3. "Altre disponibilita'" -> Appuntamenti Disponibili, ed eventualmente "Estendi area di ricerca"
//!
//! Aprire "Sposta" fa tenere lo slot proposto a quella sessione per ~40 minuti, e non c'e' modo di
//! rilasciarlo: i controlli non vanno fatti troppo spesso (il bot ne fa uno ogni 20 minuti al massimo).

use std::time::Duration;

use reqwest::blocking::Client;
use reqwest::header::CONTENT_TYPE;

use chrono::NaiveDateTime;

use crate::analisi::{self, Elenco, Riepilogo, Slot, A, AVANTI, L, RIEPILOGO};
use crate::errore::Errore;
use crate::zona::{candidata, Calendario, Zona};

pub const CUP: &str = "https://cup.isan.csi.it";
/// Tra due ricerche della stessa ricetta: ogni "Sposta" blocca un'altra data per ~40 minuti (il bot non controlla
/// piu' spesso).
pub const INTERVALLO_MIN: Duration = Duration::from_secs(20 * 60);
/// La pagina di Recupera Prenotazioni, rispetto all'indirizzo del portale.
const LISTA: &str = "/web/guest/lista-prenotazioni";
/// La pagina della ricetta: dopo "Sposta" mostra gli appuntamenti.
const RICETTA: &str = "/ricetta-dematerializzata";
const UA: &str =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36";
/// Collegarsi (TLS compreso): se il portale e' giu' non serve aspettare di piu'.
const CONNESSIONE: Duration = Duration::from_secs(20);
/// La risposta: la ricerca delle disponibilita' puo' richiedere un minuto.
const RISPOSTA: Duration = Duration::from_secs(90);

/// I campi di un POST nell'ordine del browser. Rimettere un campo ne cambia il valore al suo posto, come un
/// dizionario Python (il bot costruisce i POST cosi').
#[derive(Default, Clone)]
struct Campi(Vec<(String, String)>);

impl Campi {
    fn metti(mut self, k: impl Into<String>, v: impl Into<String>) -> Self {
        let (k, v) = (k.into(), v.into());
        match self.0.iter_mut().find(|(n, _)| *n == k) {
            Some(c) => c.1 = v,
            None => self.0.push((k, v)),
        }
        self
    }

    fn con(self, altri: Campi) -> Self {
        altri.0.into_iter().fold(self, |c, (k, v)| c.metti(k, v))
    }
}

/// I campi di un click su un pulsante ICEfaces ACE, come li manda il browser.
fn evento(sorgente: &str, evento: Option<&str>, parametro: bool) -> Campi {
    let mut c = Campi::default()
        .metti("ice.event.captured", sorgente)
        .metti("ice.event.type", "onclick")
        .metti("ice.event.x", "200")
        .metti("ice.event.y", "600");
    for k in ["alt", "ctrl", "shift", "meta", "left", "right"] {
        c = c.metti(format!("ice.event.{k}"), "false");
    }
    c = c
        .metti("javax.faces.source", sorgente)
        .metti("javax.faces.partial.execute", "@all")
        .metti("javax.faces.partial.render", "@all")
        .metti("javax.faces.partial.ajax", "true");
    match evento {
        Some(e) => c.metti("javax.faces.behavior.event", e).metti("javax.faces.partial.event", e),
        None if parametro => c.metti(sorgente, sorgente),
        None => c,
    }
}

/// Un click che il browser manda "serializzando il modulo" (il selettore della proposta, la chiusura di un avviso).
fn serializza(comando: &str) -> Campi {
    evento(A, None, false)
        .metti(comando, comando)
        .metti("javax.faces.partial.event", "click")
        .metti("ice.submit.type", "ice.s")
        .metti("ice.submit.serialization", "form")
}

/// I filtri Macrozona/Zona/Sede degli "Appuntamenti Disponibili": il browser li manda sempre, "NO_VALUE" = nessuno.
fn geo() -> Campi {
    ["macrozona", "zona", "sede"].into_iter().fold(Campi::default(), |c, n| {
        c.metti(format!("{A}:localGeoSelectorsavailable:{n}SelectMenuavailable_input"), "NO_VALUE")
    })
}

/// Lo stato JSF/ICEfaces di un modulo, per replicare i POST del browser campo per campo.
struct Modulo {
    id: String,
    url: String,
    view_state: String,
    finestra: String,
    vista: String,
}

impl Modulo {
    fn da_pagina(pagina: &str, id: &str) -> Result<Self, Errore> {
        let p = analisi::modulo(pagina, id).unwrap_or(pagina);
        let campo = |nome| analisi::nascosto(p, nome);
        match (campo("javax.faces.encodedURL"), campo("javax.faces.ViewState"), campo("ice.window"), campo("ice.view"))
        {
            (Some(url), Some(view_state), Some(finestra), Some(vista)) => {
                Ok(Modulo { id: id.into(), url, view_state, finestra, vista })
            }
            _ => Err(Errore::Inattesa(
                "Pagina CUP inattesa: campi JSF mancanti (sito cambiato o in manutenzione?)".into(),
            )),
        }
    }

    /// Un POST "partial/ajax" del modulo con questi campi, nell'ordine del browser.
    fn invia(&self, client: &Client, base: &str, campi: Campi) -> Result<String, Errore> {
        self.invia_come(client, base, &self.id, campi)
    }

    /// Come `invia`, ma a nome di un altro modulo della stessa pagina (il pulsante "Avanti" sta in un modulo suo).
    fn invia_come(&self, client: &Client, base: &str, id: &str, campi: Campi) -> Result<String, Errore> {
        if !self.url.starts_with(&format!("{base}/")) {
            return Err(Errore::Inattesa("Il portale ha indicato un indirizzo esterno: non invio dati".into()));
        }
        let dati = Campi::default()
            .metti(id, id)
            .metti("javax.faces.encodedURL", &self.url)
            .metti("ice.window", &self.finestra)
            .metti("ice.view", &self.vista)
            .con(campi)
            .metti("javax.faces.ViewState", &self.view_state);
        let corpo =
            serde_urlencoded::to_string(&dati.0).map_err(|_| Errore::Inattesa("campi non codificabili".into()))?;
        let r = client
            .post(&self.url)
            .header("Faces-Request", "partial/ajax")
            .header(CONTENT_TYPE, "application/x-www-form-urlencoded;charset=UTF-8")
            .body(corpo)
            .send()?
            .error_for_status()?;
        Ok(r.text()?)
    }
}

/// Le date che "Sposta appuntamento" offre: la proposta e gli "Appuntamenti Disponibili", dalla piu' vicina.
#[derive(Debug, Clone)]
pub struct Alternative {
    pub slots: Vec<Slot>,
    /// "Estendi area" non ha risposto in tempo: le date sono solo quelle lette fin li'.
    pub area_incompleta: bool,
}

/// Un controllo completo: la prenotazione, tutte le date offerte e quelle che vanno bene.
#[derive(Debug, Clone)]
pub struct Controllo {
    pub elenco: Elenco,
    pub slots: Vec<Slot>,
    /// Le date da proporre: nella zona, in un giorno si', prima della prenotazione (vedi zona::candidata).
    pub migliori: Vec<Slot>,
    pub area_incompleta: bool,
}

/// Recupera Prenotazioni gia' fatto: il modulo e la risposta servono per "Sposta" sulla stessa riga.
struct Lista {
    modulo: Modulo,
    xml: String,
    elenco: Elenco,
}

/// Una sessione sul portale per una ricetta (codice fiscale + numero ricetta elettronica).
pub struct Sessione {
    client: Client,
    /// L'indirizzo del portale (un finto portale locale nei test).
    base: String,
    cf: String,
    nre: String,
    lista: Option<Lista>,
    /// La pagina degli appuntamenti aperta da "Sposta" e le date lette: servono per sceglierne una.
    app: Option<Modulo>,
    slots: Vec<Slot>,
}

impl Sessione {
    /// Controlla codice fiscale e numero ricetta prima di mandarli al portale.
    pub fn nuova(cf: &str, nre: &str) -> Result<Self, Errore> {
        Self::verso(CUP, cf, nre)
    }

    /// Come `nuova`, ma ogni risposta si aspetta al massimo `attesa` (se data, e mai oltre il solito): per le
    /// letture di verifica, che devono finire entro un tempo fissato.
    pub fn con_limite(cf: &str, nre: &str, attesa: Option<Duration>) -> Result<Self, Errore> {
        Self::con_attesa(CUP, cf, nre, attesa.map_or(RISPOSTA, |a| a.min(RISPOSTA)))
    }

    pub(crate) fn verso(base: &str, cf: &str, nre: &str) -> Result<Self, Errore> {
        Self::con_attesa(base, cf, nre, RISPOSTA)
    }

    pub(crate) fn con_attesa(base: &str, cf: &str, nre: &str, risposta: Duration) -> Result<Self, Errore> {
        let (cf, nre) = analisi::credenziali(cf, nre)?;
        let client = Client::builder()
            .cookie_store(true)
            .user_agent(UA)
            .https_only(base == CUP)
            .connect_timeout(CONNESSIONE)
            .timeout(risposta)
            .build()
            .map_err(|_| Errore::Inattesa("client HTTP non disponibile".into()))?;
        Ok(Sessione { client, base: base.into(), cf, nre, lista: None, app: None, slots: vec![] })
    }

    fn pagina(&self, percorso: &str) -> Result<String, Errore> {
        Ok(self.client.get(format!("{}{percorso}", self.base)).send()?.error_for_status()?.text()?)
    }

    /// Il numero della ricetta di questa sessione (maiuscolo, senza spazi).
    pub fn nre(&self) -> &str {
        &self.nre
    }

    /// Le date lette dall'ultimo `alternative`.
    pub fn slots(&self) -> &[Slot] {
        &self.slots
    }

    /// I campi della ricerca per codice fiscale + NRE: il browser li rimanda a ogni POST dell'elenco.
    fn ricerca(&self) -> Campi {
        Campi::default()
            .metti(format!("{L}:CFInput"), &self.cf)
            .metti(format!("{L}:IDSearchTypeInput_input"), "nre-label")
            .metti(format!("{L}:IDSearchValueInput"), &self.nre)
    }

    /// L'elenco delle prenotazioni della ricetta (Recupera Prenotazioni). Solo lettura: non apre "Sposta".
    pub fn elenco(&mut self) -> Result<Elenco, Errore> {
        let modulo = Modulo::da_pagina(&self.pagina(LISTA)?, L)?;
        let sorgente = format!("{L}:filterPrescriptionNavigate");
        let campi = self
            .ricerca()
            .metti("javax.faces.source", &sorgente)
            .metti("javax.faces.partial.event", "click")
            .metti("javax.faces.partial.execute", format!("{sorgente} {L}"))
            .metti("javax.faces.partial.render", "@all")
            .metti("javax.faces.behavior.event", "action")
            .metti("javax.faces.partial.ajax", "true");
        let xml = modulo.invia(&self.client, &self.base, campi)?;
        let elenco = analisi::elenco(&xml)?;
        self.lista = Some(Lista { modulo, xml, elenco: elenco.clone() });
        self.app = None;
        self.slots.clear();
        Ok(elenco)
    }

    /// Le date offerte da "Sposta appuntamento" per la prenotazione letta con `elenco`: la proposta e gli
    /// "Appuntamenti Disponibili", premendo "Estendi area di ricerca" al piu' `estendi` volte (le aziende piu'
    /// lontane compaiono solo se hanno posti). Blocca lo slot proposto per ~40 minuti.
    pub fn alternative(&mut self, estendi: usize) -> Result<Alternative, Errore> {
        let lista = self.lista.as_ref().ok_or_else(|| Errore::Inattesa("prima va letto l'elenco".into()))?;
        let sposta = analisi::pulsante_riga(&lista.xml, lista.elenco.riga, "spostaButton")
            .ok_or_else(|| Errore::Inattesa("Pulsante 'Sposta appuntamento' non presente".into()))?;
        lista.modulo.invia(&self.client, &self.base, self.ricerca().con(evento(&sposta, Some("activate"), true)))?;
        let pagina = self.pagina(RICETTA)?; // qui il portale calcola le disponibilita': puo' essere lento
        if !pagina.contains("Appuntamenti Proposti") {
            return Err(Errore::Inattesa("Il portale non ha aperto la pagina degli appuntamenti dopo 'Sposta'".into()));
        }
        let app = Modulo::da_pagina(&pagina, A)?;
        let proposta = analisi::proposta(&pagina);
        let mut disp = String::new();
        if let Some((altre, selettore)) = analisi::altre_disponibilita(&pagina) {
            // il browser manda entrambi i click; il pannello sta nella prima risposta che lo contiene
            let primo = app.invia(&self.client, &self.base, serializza(&selettore))?;
            let secondo = app.invia(&self.client, &self.base, evento(&altre, None, true))?;
            disp = [primo, secondo].iter().find_map(|x| analisi::disponibili(x)).unwrap_or_default().to_string();
        }
        let mut area_incompleta = false;
        let volte = if disp.is_empty() { 0 } else { estendi };
        for _ in 0..volte {
            let Some(area) = analisi::estendi_area(&disp) else { break };
            let campi = geo()
                .con(evento(&area, Some("activate"), true))
                .metti("javax.faces.partial.render", format!("{A} _ricettaelettronica_WAR_cupprenotazione_:allMsgs"));
            let xml = match app.invia(&self.client, &self.base, campi) {
                // il portale e' lento proprio ad allargare l'area: valgono le date gia' lette
                Err(Errore::Timeout) => {
                    area_incompleta = true;
                    break;
                }
                altro => altro?,
            };
            let Some(nuovo) = analisi::disponibili(&xml) else { break };
            disp = nuovo.to_string();
            if let Some(pulisci) = analisi::chiudi_avviso(&xml) {
                // come il browser: chiude l'avviso "area estesa"
                app.invia(&self.client, &self.base, geo().con(serializza(&pulisci)))?;
            }
        }
        let mut slots: Vec<Slot> = proposta.iter().cloned().collect();
        slots.extend(analisi::date_disponibili(&disp, proposta.as_ref()));
        slots.sort_by_key(|s| s.quando);
        self.app = Some(app);
        self.slots = slots.clone();
        Ok(Alternative { slots, area_incompleta })
    }

    /// Sceglie la data (il suo "Seleziona"; la proposta e' gia' selezionata) e preme "Avanti" fino al Riepilogo.
    /// Non conferma nulla.
    pub fn riepilogo(&mut self, slot: &Slot) -> Result<Riepilogo, Errore> {
        let app = self.app.as_ref().ok_or_else(|| Errore::Inattesa("prima vanno lette le date".into()))?;
        if slot.seleziona.is_none() && !slot.proposta {
            return Err(Errore::Inattesa("Questa data non ha un pulsante 'Seleziona': non posso sceglierla".into()));
        }
        if let Some(sel) = &slot.seleziona {
            let xml = app.invia(&self.client, &self.base, geo().con(evento(sel, None, true)))?;
            if xml.contains("alert-danger") {
                let e = analisi::errori(&xml);
                return Err(Errore::Inattesa(format!(
                    "Il portale ha rifiutato la selezione della data{}",
                    e.first().map_or(String::new(), |e| format!(": {e}"))
                )));
            }
        }
        let avanti = format!("{AVANTI}:appuntamenti-nextButton-main");
        let xml = app.invia_come(&self.client, &self.base, AVANTI, evento(&avanti, None, true))?;
        let pagina = match analisi::redirect(&xml) {
            Some(url) if !url.starts_with(&format!("{}/", self.base)) => {
                return Err(Errore::Inattesa("Il portale ha indicato un indirizzo esterno: non proseguo".into()))
            }
            Some(url) => self.client.get(url).send()?.error_for_status()?.text()?,
            None => xml.clone(),
        };
        if !pagina.contains("Riepilogo") || !pagina.contains(&format!("{RIEPILOGO}:riepilogo-nextButton-bottom")) {
            let e = analisi::errori(&xml).into_iter().chain(analisi::errori(&pagina)).next();
            return Err(Errore::Inattesa(format!(
                "Non sono arrivato al Riepilogo{}",
                e.map_or(String::new(), |e| format!(": {e}"))
            )));
        }
        Ok(analisi::riepilogo(pagina))
    }

    /// I campi del modulo del Riepilogo, senza quelli dello stato JSF (li aggiunge `Modulo::invia`).
    fn campi_riepilogo(pagina: &str) -> Campi {
        let via = [RIEPILOGO, "javax.faces.encodedURL", "ice.window", "ice.view", "javax.faces.ViewState"];
        Campi(
            analisi::campi_modulo(pagina, RIEPILOGO).into_iter().filter(|(k, _)| !via.contains(&k.as_str())).collect(),
        )
    }

    /// Se il Riepilogo ha delle note, il portale vuole "Conferma presa visione" prima di "Conferma" (dal vivo,
    /// 2026-10-08: senza, risponde "Non e' stata effettuata la presa visione delle note" e non prenota). Ritorna il
    /// ViewState nuovo, se il portale ne da' uno. Err se il click non risulta registrato: niente e' ancora partito.
    pub fn presa_visione(&self, r: &Riepilogo) -> Result<Option<String>, Errore> {
        let Some(id) = analisi::presa_visione(&r.pagina)? else { return Ok(None) };
        let modulo = Modulo::da_pagina(&r.pagina, RIEPILOGO)?;
        let xml =
            modulo.invia(&self.client, &self.base, Self::campi_riepilogo(&r.pagina).con(evento(&id, None, true)))?;
        if !analisi::presa_visione_registrata(&xml) {
            return Err(Errore::Inattesa(
                "Il portale non ha registrato la presa visione delle note: non confermo".into(),
            ));
        }
        Ok(analisi::view_state(&xml))
    }

    /// Preme "Conferma" sul Riepilogo. Da qui la prenotazione puo' essere cambiata anche se la risposta non arriva.
    pub fn conferma(&self, r: &Riepilogo, view_state: Option<String>) -> Result<String, Errore> {
        let mut modulo = Modulo::da_pagina(&r.pagina, RIEPILOGO)?;
        if let Some(vs) = view_state {
            modulo.view_state = vs; // dopo la presa visione il portale ne ha dato uno nuovo
        }
        let conferma = format!("{RIEPILOGO}:riepilogo-nextButton-bottom");
        modulo.invia(&self.client, &self.base, Self::campi_riepilogo(&r.pagina).con(evento(&conferma, None, true)))
    }

    /// Preme "Disdici" sulla prenotazione di questa data (e di questo luogo, `Luogo::chiave`, se dato: due
    /// appuntamenti possono cadere alla stessa ora in sedi diverse) e ritorna il pulsante "Si'" del dialogo di
    /// conferma, senza premerlo.
    pub fn disdici_dialogo(&mut self, quando: NaiveDateTime, luogo: Option<&str>) -> Result<String, Errore> {
        let ricerca = self.ricerca();
        let lista = self.lista.as_mut().ok_or_else(|| Errore::Inattesa("prima va letto l'elenco".into()))?;
        let e = &lista.elenco;
        let righe: Vec<usize> = e
            .prenotate
            .iter()
            .zip(&e.righe)
            .filter(|(p, _)| p.quando == quando && luogo.is_none_or(|l| l == p.luogo.chiave()))
            .map(|(_, r)| *r)
            .collect();
        let [riga] = righe[..] else {
            return Err(Errore::Inattesa("Non trovo un'unica prenotazione a quella data: non disdico".into()));
        };
        let bottone = analisi::pulsante_riga(&lista.xml, riga, "disdiciButton")
            .ok_or_else(|| Errore::Inattesa("Pulsante 'Disdici' non presente".into()))?;
        let xml =
            lista.modulo.invia(&self.client, &self.base, ricerca.con(evento(&bottone, Some("activate"), true)))?;
        if let Some(vs) = analisi::view_state(&xml) {
            lista.modulo.view_state = vs;
        }
        analisi::dialogo_si(&xml)
    }

    /// Preme "Si'" nel dialogo della disdetta. Da qui la disdetta puo' essere partita anche se la risposta non arriva.
    pub fn premi_si(&self, si: &str) -> Result<String, Errore> {
        let lista = self.lista.as_ref().ok_or_else(|| Errore::Inattesa("prima va letto l'elenco".into()))?;
        lista.modulo.invia(&self.client, &self.base, self.ricerca().con(evento(si, Some("activate"), true)))
    }

    /// Un controllo come quelli del bot: la prenotazione, le date offerte e quelle che vanno bene.
    pub fn controlla(&mut self, zona: &Zona, cal: &Calendario) -> Result<Controllo, Errore> {
        let elenco = self.elenco()?;
        // due prenotazioni della stessa prestazione: come il bot, prima si sistema e poi si cerca. "Sposta"
        // bloccherebbe una data per niente, e le date andrebbero confrontate con l'appuntamento piu' vicino
        let adesso = chrono::Local::now().naive_local();
        if let Some((tenere, disdire)) = analisi::doppione(&elenco.prenotate, adesso) {
            if elenco.n_prenotate == elenco.prenotate.len() {
                return Err(Errore::Doppione(format!(
                    "La ricetta ha due prenotazioni di {}: {} e {}. Prima va disdetta quella del {} \
                     (dal sito del CUP o al call center): non apro \"Sposta\".",
                    tenere.cosa,
                    analisi::breve(&tenere.quando),
                    analisi::breve(&disdire.quando),
                    analisi::breve(&disdire.quando)
                )));
            }
        }
        let alt = self.alternative(zona.estensioni())?;
        let migliori = alt.slots.iter().filter(|s| candidata(s, Some(&elenco.attuale), zona, cal)).cloned().collect();
        Ok(Controllo { elenco, slots: alt.slots, migliori, area_incompleta: alt.area_incompleta })
    }
}

#[cfg(test)]
mod test {
    use std::net::TcpListener;

    use super::*;
    use crate::prova::{campi, portale};

    /// La pagina di Recupera Prenotazioni con lo stato JSF; `azione` e' dove il portale dice di mandare il POST.
    fn pagina_lista(azione: &str) -> String {
        format!(
            r#"<html><form id="{L}" action="x"><input type="hidden" name="javax.faces.encodedURL" value="{azione}" /><input type="hidden" name="javax.faces.ViewState" value="vs&amp;1" /><input type="hidden" name="ice.window" value="w1" /><input type="hidden" name="ice.view" value="v1" /></form></html>"#
        )
    }

    const ELENCO: &str =
        "<partial-response><![CDATA[<span>Stato:</span> PRENOTATO <span>Quando:</span><span>Giovedì 7 \
        Gennaio 2027</span><span>alle ore</span><span>11:45</span><span>Dove: </span><div><span>MOLINETTE</span> </div>\
        <div><span>AMB 1</span> </div>]]></partial-response>";

    fn sessione(base: &str) -> Sessione {
        Sessione::verso(base, "rssmra80a01l219x", "010A0 0000000001").unwrap()
    }

    #[test]
    fn recupera_prenotazioni_come_il_browser() {
        let (base, h) =
            portale(|b| vec![(200, pagina_lista(&format!("{b}/web/guest/lista?p=1"))), (200, ELENCO.into())]);
        let e = sessione(&base).elenco().unwrap();
        assert_eq!(e.attuale.luogo.sede, "MOLINETTE");
        assert_eq!(analisi::breve(&e.attuale.quando), "gio 07/01/2027 ore 11:45");
        let viste = h.join().unwrap();
        assert_eq!(viste[0].0, "GET /web/guest/lista-prenotazioni HTTP/1.1");
        assert!(viste[0].1.contains("user-agent: mozilla/5.0"));
        let (riga, intest, corpo) = &viste[1];
        assert_eq!(riga, "POST /web/guest/lista?p=1 HTTP/1.1");
        assert!(intest.contains("faces-request: partial/ajax"));
        assert!(intest.contains("content-type: application/x-www-form-urlencoded;charset=utf-8"));
        assert!(intest.contains("cookie: jsessionid=abc"), "il cookie della pagina torna nel POST");
        let c = campi(corpo);
        let nomi: Vec<&str> = c.iter().map(|(k, _)| k.as_str()).collect();
        // l'ordine del browser: modulo e stato JSF prima, i campi, il ViewState per ultimo
        assert_eq!(nomi[..4], [L, "javax.faces.encodedURL", "ice.window", "ice.view"]);
        assert_eq!(c.last().unwrap(), &("javax.faces.ViewState".to_string(), "vs&1".to_string()));
        let valore = |k: &str| c.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str());
        assert_eq!(valore(&format!("{L}:CFInput")), Some("RSSMRA80A01L219X"));
        assert_eq!(valore(&format!("{L}:IDSearchValueInput")), Some("010A00000000001"));
        assert_eq!(valore(&format!("{L}:IDSearchTypeInput_input")), Some("nre-label"));
        assert_eq!(valore("javax.faces.source"), Some(format!("{L}:filterPrescriptionNavigate").as_str()));
    }

    #[test]
    fn nessun_dato_verso_un_indirizzo_esterno() {
        // la pagina dice di mandare il modulo altrove: niente POST, solo la GET della pagina
        let (base, h) = portale(|_| vec![(200, pagina_lista("https://esempio.invalid/raccogli"))]);
        assert!(matches!(sessione(&base).elenco(), Err(Errore::Inattesa(m)) if m.contains("esterno")));
        assert_eq!(h.join().unwrap().len(), 1);
    }

    #[test]
    fn pagina_senza_stato_jsf_o_errori_http() {
        let (base, h) = portale(|_| vec![(200, "<html>Manutenzione</html>".into())]);
        assert!(matches!(sessione(&base).elenco(), Err(Errore::Inattesa(m)) if m.contains("campi JSF")));
        h.join().unwrap();
        let (base, h) = portale(|_| vec![(503, "giu'".into())]);
        assert!(matches!(sessione(&base).elenco(), Err(Errore::Sovraccarico(m)) if m == "HTTP 503"));
        h.join().unwrap();
        let (base, h) = portale(|_| vec![(403, "no".into())]);
        assert!(matches!(sessione(&base).elenco(), Err(Errore::Inattesa(m)) if m.starts_with("HTTP 403")));
        h.join().unwrap();
    }

    #[test]
    fn portale_spento() {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", l.local_addr().unwrap());
        drop(l); // nessuno ascolta piu' su quella porta
        assert!(matches!(sessione(&base).elenco(), Err(Errore::Sovraccarico(_))));
    }

    use crate::prova::{appuntamenti, appuntamento, disponibili, elenco_prenotato, modulo};
    use crate::zona::{Calendario, Dove, Zona};

    fn data(quando: &str, sede: &str, citta: &str, id: &str) -> String {
        appuntamento(quando, sede, "AMB", citta, Some(&format!("{A}:{id}")))
    }

    /// Le risposte del portale fino agli "Appuntamenti Disponibili" (prima di "Estendi area").
    fn fino_alle_disponibili(b: &str, estese: Vec<(u16, String)>) -> Vec<(u16, String)> {
        let proposta = appuntamento("Giovedì 10 Dicembre 2026", "MOLINETTE", "AMB", "TORINO (TO)", None);
        let mut r = vec![
            (200, modulo(L, &format!("{b}/lista?p=1"), "")),
            (200, elenco_prenotato("Giovedì 7 Gennaio 2027", "MOLINETTE", "TORINO (TO)")),
            (200, "<partial-response>sposta</partial-response>".into()),
            (200, appuntamenti(&format!("{b}/ricetta?azione"), &proposta)),
            (200, "<partial-response>selettore</partial-response>".into()),
            (
                200,
                disponibili(
                    &[
                        proposta.clone(),
                        data("Venerdì 4 Dicembre 2026", "MAURIZIANO", "TORINO (TO)", "s1"),
                        data("Giovedì 3 Dicembre 2026", "SANTA CROCE", "MONCALIERI (TO)", "s2"),
                    ],
                    true,
                    false,
                ),
            ),
        ];
        r.extend(estese);
        r
    }

    #[test]
    fn sposta_altre_disponibilita_ed_estendi_area() {
        let (base, h) = portale(|b| {
            let lontane = disponibili(
                &[
                    data("Venerdì 4 Dicembre 2026", "MAURIZIANO", "TORINO (TO)", "s1"),
                    data("Giovedì 3 Dicembre 2026", "SANTA CROCE", "MONCALIERI (TO)", "s2"),
                    data("Mercoledì 2 Dicembre 2026", "CARDINAL MASSAIA", "ASTI (AT)", "s3"),
                    data("Martedì 1 Dicembre 2026", "SAN GIOVANNI BOSCO", "TORINO (TO)", "s4"),
                ],
                false,
                true,
            );
            fino_alle_disponibili(
                b,
                vec![(200, lontane), (200, "<partial-response>avviso chiuso</partial-response>".into())],
            )
        });
        let mut s = sessione(&base);
        let zona = Zona::new(Dove::Comune(String::new())); // il comune della prenotazione: Torino
        let cal = Calendario { no_settimana: vec![4], ..Default::default() }; // niente venerdi'
        let c = s.controlla(&zona, &cal).unwrap();
        let viste = h.join().unwrap();
        let sedi = |v: &[Slot]| v.iter().map(|x| x.luogo.sede.clone()).collect::<Vec<_>>();
        // tutte le date, dalla piu' vicina; la proposta una volta sola
        assert_eq!(
            sedi(&c.slots),
            ["SAN GIOVANNI BOSCO", "CARDINAL MASSAIA", "SANTA CROCE", "MAURIZIANO", "MOLINETTE"]
        );
        assert!(c.slots[4].proposta && !c.area_incompleta);
        // a Torino, non di venerdi', prima del 7 gennaio: la proposta e San Giovanni Bosco
        assert_eq!(sedi(&c.migliori), ["SAN GIOVANNI BOSCO", "MOLINETTE"]);
        // le richieste, come il browser: elenco, Sposta, pagina, i due click, un'estensione e la chiusura dell'avviso
        let righe: Vec<&str> = viste.iter().map(|v| v.0.as_str()).collect();
        assert_eq!(
            righe,
            [
                "GET /web/guest/lista-prenotazioni HTTP/1.1",
                "POST /lista?p=1 HTTP/1.1",
                "POST /lista?p=1 HTTP/1.1",
                "GET /ricetta-dematerializzata HTTP/1.1",
                "POST /ricetta?azione HTTP/1.1",
                "POST /ricetta?azione HTTP/1.1",
                "POST /ricetta?azione HTTP/1.1",
                "POST /ricetta?azione HTTP/1.1",
            ]
        );
        let valore = |i: usize, k: &str| campi(&viste[i].2).into_iter().find(|(n, _)| n == k).map(|(_, v)| v);
        // Sposta sulla riga della prenotazione, con la ricerca rimandata
        assert_eq!(valore(2, "javax.faces.source"), Some(format!("{L}:righe:0:spostaButton")));
        assert_eq!(valore(2, &format!("{L}:CFInput")).as_deref(), Some("RSSMRA80A01L219X"));
        // il selettore della proposta, poi "Altre disponibilita'"
        assert_eq!(valore(4, &format!("{A}:x:0:app_selector")), Some(format!("{A}:x:0:app_selector")));
        assert_eq!(valore(4, "ice.submit.type").as_deref(), Some("ice.s"));
        assert_eq!(valore(5, "javax.faces.source"), Some(format!("{A}:altre")));
        // "Estendi area": filtri geografici vuoti e il render del browser, al posto di "@all"
        let estendi = campi(&viste[6].2);
        assert_eq!(valore(6, "javax.faces.source"), Some(format!("{A}:nextArea")));
        assert_eq!(
            valore(6, &format!("{A}:localGeoSelectorsavailable:zonaSelectMenuavailable_input")).as_deref(),
            Some("NO_VALUE")
        );
        let render: Vec<_> = estendi.iter().filter(|(n, _)| n == "javax.faces.partial.render").collect();
        assert_eq!(render.len(), 1);
        assert_eq!(render[0].1, format!("{A} _ricettaelettronica_WAR_cupprenotazione_:allMsgs"));
        assert_eq!(valore(7, &format!("{A}:notifyCleaner")), Some(format!("{A}:notifyCleaner")));
    }

    #[test]
    fn estendi_area_lento_tiene_le_date_gia_lette() {
        // dal vivo (2026-09-28): "Estendi area" in provincia di Torino scadeva sempre e il controllo perdeva tutto
        let (base, h) = portale(|b| fino_alle_disponibili(b, vec![(0, String::new())]));
        let mut s =
            Sessione::con_attesa(&base, "RSSMRA80A01L219X", "010A00000000001", Duration::from_millis(800)).unwrap();
        // l'attesa breve vale per tutte le richieste: le altre rispondono subito
        let c = s.controlla(&Zona::new(Dove::Provincia(String::new())), &Calendario::default()).unwrap();
        assert!(c.area_incompleta);
        assert_eq!(c.slots.len(), 3);
        assert_eq!(h.join().unwrap().len(), 7);
    }

    #[test]
    fn sede_e_tutte_non_estendono_l_area() {
        for dove in [Dove::Tutte, Dove::Sede(String::new())] {
            let (base, h) = portale(|b| fino_alle_disponibili(b, vec![]));
            let c = sessione(&base).controlla(&Zona::new(dove.clone()), &Calendario::default()).unwrap();
            assert_eq!(h.join().unwrap().len(), 6, "{dove:?}");
            // Tutte: le tre date prima del 7 gennaio; Sede: solo la proposta, che e' alle Molinette
            assert_eq!(c.migliori.len(), if dove == Dove::Tutte { 3 } else { 1 });
        }
    }

    #[test]
    fn pannello_gia_nella_prima_risposta_il_secondo_click_parte_lo_stesso() {
        let (base, h) = portale(|b| {
            let mut r = fino_alle_disponibili(b, vec![]);
            let pannello = r.pop().unwrap();
            r.pop(); // la risposta al selettore diventa il pannello
            r.push(pannello);
            r.push((200, "<partial-response>altro</partial-response>".into()));
            r
        });
        let c = sessione(&base).controlla(&Zona::new(Dove::Tutte), &Calendario::default()).unwrap();
        assert_eq!((h.join().unwrap().len(), c.slots.len()), (6, 3));
    }

    #[test]
    fn chiusura_dell_avviso_come_il_browser() {
        let (base, h) = portale(|b| {
            let estesa =
                disponibili(&[data("Martedì 1 Dicembre 2026", "SAN GIOVANNI BOSCO", "TORINO (TO)", "s4")], false, true);
            fino_alle_disponibili(b, vec![(200, estesa), (200, "<partial-response/>".into())])
        });
        sessione(&base).controlla(&Zona::new(Dove::Comune(String::new())), &Calendario::default()).unwrap();
        let viste = h.join().unwrap();
        let nomi: Vec<String> = campi(&viste[7].2).into_iter().map(|(n, _)| n).collect();
        let geo: Vec<String> = ["macrozona", "zona", "sede"]
            .iter()
            .map(|n| format!("{A}:localGeoSelectorsavailable:{n}SelectMenuavailable_input"))
            .collect();
        // modulo e stato JSF, i filtri geografici, il click "serializzato", il comando, il ViewState per ultimo
        assert_eq!(nomi[..4], [A, "javax.faces.encodedURL", "ice.window", "ice.view"]);
        assert_eq!(nomi[4..7], geo[..]);
        assert_eq!(nomi[7], "ice.event.captured");
        let fine: Vec<&str> = nomi[nomi.len() - 5..].iter().map(String::as_str).collect();
        assert_eq!(
            fine,
            [
                &format!("{A}:notifyCleaner") as &str,
                "javax.faces.partial.event",
                "ice.submit.type",
                "ice.submit.serialization",
                "javax.faces.ViewState"
            ]
        );
    }

    #[test]
    fn doppione_non_apre_sposta() {
        let (base, h) = portale(|b| {
            let due = format!(
                "<partial-response><![CDATA[<span>Stato:</span> PRENOTATO {}<div id=\"{L}:r:0:spostaButton\"></div>\
                 <span>Stato:</span> PRENOTATO {}]]></partial-response>",
                appuntamento("Giovedì 7 Gennaio 2099", "MOLINETTE", "AMB", "TORINO (TO)", None),
                appuntamento("Martedì 1 Dicembre 2099", "MAURIZIANO", "AMB", "TORINO (TO)", None),
            );
            vec![(200, modulo(L, &format!("{b}/lista"), "")), (200, due)]
        });
        let e = sessione(&base).controlla(&Zona::new(Dove::Tutte), &Calendario::default()).unwrap_err();
        // si tiene la piu' vicina (7 gennaio), si disdice l'altra
        assert!(
            matches!(&e, Errore::Doppione(m) if m.contains("quella del") && m.contains("01/12/2099 ore 09:00 (dal sito")),
            "{e}"
        );
        assert_eq!(h.join().unwrap().len(), 2); // niente "Sposta"
    }

    #[test]
    fn indirizzo_esterno_anche_negli_appuntamenti() {
        let (base, h) = portale(|b| {
            let mut r = fino_alle_disponibili(b, vec![]);
            r.truncate(4);
            let proposta = appuntamento("Giovedì 10 Dicembre 2026", "MOLINETTE", "AMB", "TORINO (TO)", None);
            r[3] = (200, appuntamenti("https://esempio.invalid/raccogli", &proposta));
            r
        });
        let e = sessione(&base).controlla(&Zona::new(Dove::Tutte), &Calendario::default()).unwrap_err();
        assert!(matches!(&e, Errore::Inattesa(m) if m.contains("esterno")));
        assert_eq!(h.join().unwrap().len(), 4); // nessun POST verso l'esterno
    }

    #[test]
    fn sposta_senza_appuntamenti_o_senza_pulsante() {
        let (base, h) = portale(|b| {
            vec![
                (200, modulo(L, &format!("{b}/lista"), "")),
                (200, elenco_prenotato("Giovedì 7 Gennaio 2027", "MOLINETTE", "TORINO (TO)")),
                (200, "<partial-response/>".into()),
                (200, "<html>Sessione scaduta</html>".into()),
            ]
        });
        let mut s = sessione(&base);
        assert!(matches!(s.controlla(&Zona::new(Dove::Tutte), &Calendario::default()),
            Err(Errore::Inattesa(m)) if m.contains("dopo 'Sposta'")));
        h.join().unwrap();
        let (base, h) = portale(|b| {
            let senza =
                elenco_prenotato("Giovedì 7 Gennaio 2027", "MOLINETTE", "TORINO (TO)").replace("spostaButton", "altro");
            vec![(200, modulo(L, &format!("{b}/lista"), "")), (200, senza)]
        });
        assert!(matches!(sessione(&base).controlla(&Zona::new(Dove::Tutte), &Calendario::default()),
            Err(Errore::Inattesa(m)) if m.contains("Sposta")));
        assert_eq!(h.join().unwrap().len(), 2); // nessun "Sposta" premuto
        assert!(matches!(sessione("http://127.0.0.1:9").alternative(0), Err(Errore::Inattesa(_))));
        // elenco non letto
    }

    #[test]
    fn credenziali_sbagliate_non_partono() {
        assert!(matches!(Sessione::nuova("ABC", "010A00000000001"), Err(Errore::DatiNonValidi(_))));
    }
}
