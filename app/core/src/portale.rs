//! La sessione sul portale: cookie, moduli JSF/ICEfaces e le richieste come le manda il browser.

use std::time::Duration;

use reqwest::blocking::Client;
use reqwest::header::CONTENT_TYPE;

use crate::analisi::{self, Elenco};
use crate::errore::Errore;

pub const CUP: &str = "https://cup.isan.csi.it";
/// La pagina di Recupera Prenotazioni, rispetto all'indirizzo del portale.
const LISTA: &str = "/web/guest/lista-prenotazioni";
const UA: &str =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36";
/// Il modulo di Recupera Prenotazioni.
const L: &str = "_listaprenotazioni_WAR_cupprenotazione_:prescrizioniForm";
/// Collegarsi (TLS compreso): se il portale e' giu' non serve aspettare di piu'.
const CONNESSIONE: Duration = Duration::from_secs(20);
/// La risposta: la ricerca delle disponibilita' puo' richiedere un minuto.
const RISPOSTA: Duration = Duration::from_secs(90);

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
    fn invia(&self, client: &Client, base: &str, campi: &[(&str, &str)]) -> Result<String, Errore> {
        if !self.url.starts_with(&format!("{base}/")) {
            return Err(Errore::Inattesa("Il portale ha indicato un indirizzo esterno: non invio dati".into()));
        }
        let mut dati: Vec<(&str, &str)> = vec![
            (&self.id, &self.id),
            ("javax.faces.encodedURL", &self.url),
            ("ice.window", &self.finestra),
            ("ice.view", &self.vista),
        ];
        dati.extend_from_slice(campi);
        dati.push(("javax.faces.ViewState", &self.view_state));
        let corpo =
            serde_urlencoded::to_string(&dati).map_err(|_| Errore::Inattesa("campi non codificabili".into()))?;
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

/// Una sessione sul portale per una ricetta (codice fiscale + numero ricetta elettronica).
pub struct Sessione {
    client: Client,
    /// L'indirizzo del portale (un finto portale locale nei test).
    base: String,
    cf: String,
    nre: String,
}

impl Sessione {
    /// Controlla codice fiscale e numero ricetta prima di mandarli al portale.
    pub fn nuova(cf: &str, nre: &str) -> Result<Self, Errore> {
        Self::verso(CUP, cf, nre)
    }

    fn verso(base: &str, cf: &str, nre: &str) -> Result<Self, Errore> {
        let (cf, nre) = analisi::credenziali(cf, nre)?;
        let client = Client::builder()
            .cookie_store(true)
            .user_agent(UA)
            .https_only(base == CUP)
            .connect_timeout(CONNESSIONE)
            .timeout(RISPOSTA)
            .build()
            .map_err(|_| Errore::Inattesa("client HTTP non disponibile".into()))?;
        Ok(Sessione { client, base: base.into(), cf, nre })
    }

    fn pagina(&self, url: &str) -> Result<String, Errore> {
        Ok(self.client.get(url).send()?.error_for_status()?.text()?)
    }

    /// L'elenco delle prenotazioni della ricetta (Recupera Prenotazioni). Solo lettura: non apre "Sposta".
    pub fn elenco(&self) -> Result<Elenco, Errore> {
        let lista = Modulo::da_pagina(&self.pagina(&format!("{}{LISTA}", self.base))?, L)?;
        let cf_campo = format!("{L}:CFInput");
        let tipo_campo = format!("{L}:IDSearchTypeInput_input");
        let nre_campo = format!("{L}:IDSearchValueInput");
        let sorgente = format!("{L}:filterPrescriptionNavigate");
        let esegui = format!("{sorgente} {L}");
        let xml = lista.invia(
            &self.client,
            &self.base,
            &[
                (&cf_campo, &self.cf),
                (&tipo_campo, "nre-label"),
                (&nre_campo, &self.nre),
                ("javax.faces.source", &sorgente),
                ("javax.faces.partial.event", "click"),
                ("javax.faces.partial.execute", &esegui),
                ("javax.faces.partial.render", "@all"),
                ("javax.faces.behavior.event", "action"),
                ("javax.faces.partial.ajax", "true"),
            ],
        )?;
        analisi::elenco(&xml)
    }
}

#[cfg(test)]
mod test {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::thread;

    use super::*;

    /// (riga iniziale, intestazioni in minuscolo, corpo) di una richiesta ricevuta dal finto portale.
    type Vista = (String, String, String);

    /// Un finto portale su 127.0.0.1: risponde con le pagine che `risposte` costruisce dal suo indirizzo, una
    /// per richiesta, e restituisce le richieste ricevute.
    fn portale(risposte: impl FnOnce(&str) -> Vec<(u16, String)>) -> (String, thread::JoinHandle<Vec<Vista>>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", l.local_addr().unwrap());
        let risposte = risposte(&base);
        let h = thread::spawn(move || {
            let mut viste = vec![];
            for (codice, corpo) in risposte {
                let (mut c, _) = l.accept().unwrap();
                let mut r = BufReader::new(c.try_clone().unwrap());
                let (mut riga, mut intest, mut lunghezza) = (String::new(), String::new(), 0);
                r.read_line(&mut riga).unwrap();
                loop {
                    let mut h = String::new();
                    r.read_line(&mut h).unwrap();
                    if h.trim().is_empty() {
                        break;
                    }
                    let h = h.to_lowercase();
                    if let Some(v) = h.strip_prefix("content-length:") {
                        lunghezza = v.trim().parse().unwrap();
                    }
                    intest.push_str(&h);
                }
                let mut b = vec![0; lunghezza];
                r.read_exact(&mut b).unwrap();
                viste.push((riga.trim().to_string(), intest, String::from_utf8(b).unwrap()));
                write!(
                    c,
                    "HTTP/1.1 {codice} X\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n\
                     Set-Cookie: JSESSIONID=abc; Path=/\r\nConnection: close\r\n\r\n{corpo}",
                    corpo.len()
                )
                .unwrap();
            }
            viste
        });
        (base, h)
    }

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

    #[test]
    fn credenziali_sbagliate_non_partono() {
        assert!(matches!(Sessione::nuova("ABC", "010A00000000001"), Err(Errore::DatiNonValidi(_))));
    }

    fn campi(corpo: &str) -> Vec<(String, String)> {
        serde_urlencoded::from_str(corpo).unwrap()
    }
}
