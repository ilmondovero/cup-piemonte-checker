//! Segnalazioni dei problemi allo sviluppatore, solo se l'utente le ha accese (all'inizio sono spente).
//!
//! - Dove: l'indirizzo del server (solo https) entra nell'app quando si compila (`CUP_SEGNALAZIONI_URL`, un segreto
//!   del repository): non e' nel codice pubblico, ma e' dentro l'app installata e chi la smonta lo trova. Il server
//!   ha i suoi limiti. Senza indirizzo l'opzione non c'e' e non parte mai nulla.
//! - Cosa: versione, sistema, tipo di installazione, che cosa e' successo (una frase fissa, scritta qui) e le ultime
//!   righe del registro. Per un panico solo il punto del codice (file e riga): ne' il messaggio (puo' contenere
//!   dati) ne' la pila (in release i simboli non ci sono, e su Windows leggerli puo' andare in rete). Nomi e luoghi
//!   non si riconoscono: restano fuori perche' il registro non li scrive mai. Prima di partire il rapporto passa
//!   anche da `pulito`: codici fiscali, numeri di ricetta, date, orari, email, telefoni e percorsi della cartella
//!   dell'utente diventano segnaposto.
//! - Quando: un panico (scritto su un file dal gancio del panico, senza rete, e mandato all'avvio dopo), un avvio
//!   non riuscito, un aggiornamento non riuscito che conta, una pagina del portale non riconosciuta due controlli di
//!   fila, un esito incerto dopo una conferma o una disdetta.
//! - Quante: lo stesso tipo al massimo una volta ogni 6 ore, la stessa frase una volta al giorno, 10 al giorno, 5 in
//!   attesa (con la coda piena si butta la piu' vecchia). Si mandano da un thread a parte: niente qui deve fermare controlli, operazioni, avvio o aggiornamenti.
//! - Rete che non va (o il server che dice "troppe"): si riprova al prossimo avvio, al massimo TENTATIVI volte.
//!
//! L'identificativo dell'installazione e' un numero a caso, scelto la prima volta: non dice nulla dell'utente o del
//! computer.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::Duration;

use chacha20poly1305::aead::rand_core::RngCore;
use chacha20poly1305::aead::OsRng;
use chrono::{Local, NaiveDateTime, TimeZone};
use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::aggiornamenti::senza_percorsi;
use crate::controlli::{adesso, blocca};
use crate::registro::Registro;

const FILE: &str = "segnalazioni.json";
/// Le segnalazioni scritte dal gancio del panico: si mandano all'avvio dopo.
const FILE_PANICO: &str = "segnalazioni-panico.json";
/// Lo stesso tipo di problema al massimo una volta ogni...
const STESSO_TIPO: i64 = 6 * 3600;
/// La stessa frase al massimo una volta ogni...
const STESSO_TESTO: i64 = 24 * 3600;
const AL_GIORNO: usize = 10;
/// Segnalazioni in attesa di partire, al massimo.
pub const CODA: usize = 5;
/// Invii non riusciti per la rete prima di lasciar perdere una segnalazione.
pub const TENTATIVI: u32 = 3;
/// Righe del registro nel rapporto.
const RIGHE: usize = 80;
/// Il rapporto, al massimo.
const RAPPORTO: usize = 24_000;
/// La richiesta intera, al massimo: il server rifiuta quelle piu' grandi.
const CORPO: usize = 32 * 1024;
const TESTO: usize = 200;
const ATTESA_RETE: Duration = Duration::from_secs(20);
/// Le segnalazioni rimaste in attesa partono un po' dopo l'avvio: l'avvio deve restare leggero.
const PRIMO: Duration = Duration::from_secs(30);

pub const CHIUSA: &str = "L'app si e' chiusa per un errore interno";
pub const INTERNO: &str = "Errore interno dell'app (l'app ha continuato)";
pub const NON_PARTE: &str = "L'app non riesce a partire";
pub const PORTALE: &str = "Pagina del portale non riconosciuta (2 volte di fila)";
pub const INCERTO: &str = "Esito incerto dopo una conferma";
pub const INCERTA_DISDETTA: &str = "Esito incerto dopo una disdetta";

/// "Aggiornamento alla versione 0.3.2 non riuscito".
pub fn aggiornamento_non_riuscito(versione: &str) -> String {
    format!("Aggiornamento alla versione {versione} non riuscito")
}

/// L'indirizzo del server, se l'app e' stata compilata con uno (un segreto vuoto vale come nessuno).
pub fn indirizzo() -> Option<&'static str> {
    valido(option_env!("CUP_SEGNALAZIONI_URL"))
}

/// Solo un indirizzo https: qualunque altra cosa vale come nessuno.
fn valido(u: Option<&'static str>) -> Option<&'static str> {
    u.map(str::trim).filter(|u| u.starts_with("https://") && u.len() > "https://".len())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Tipo {
    Panico,
    Avvio,
    Aggiornamento,
    Portale,
    Errore,
}

impl Tipo {
    pub fn nome(self) -> &'static str {
        match self {
            Tipo::Panico => "panico",
            Tipo::Avvio => "avvio",
            Tipo::Aggiornamento => "aggiornamento",
            Tipo::Portale => "portale",
            Tipo::Errore => "errore",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Segnalazione {
    pub tipo: Tipo,
    /// Una frase fissa scritta dall'app, mai testi del portale o delle librerie.
    pub testo: String,
    /// Gia' passato da `pulito`.
    pub rapporto: String,
    pub ts: i64,
    /// Invii non riusciti per la rete.
    #[serde(default)]
    pub tentativi: u32,
}

/// Cosa l'app ricorda delle segnalazioni, in chiaro (nessun dato personale) in `segnalazioni.json`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Memoria {
    /// L'utente le ha accese.
    pub accese: bool,
    /// 16 cifre esadecimali a caso, scelte la prima volta.
    pub id: String,
    /// Le segnalazioni accettate nelle ultime 24 ore (tipo, frase, quando), per i limiti.
    pub fatte: Vec<(Tipo, String, i64)>,
    pub in_attesa: Vec<Segnalazione>,
    /// L'ultima inviata: quando e di che tipo.
    pub ultima: Option<(i64, Tipo)>,
}

impl Memoria {
    /// Mette in coda la segnalazione, se i limiti lo permettono; se no dice perche' (per il registro). Con la coda
    /// piena (per esempio dopo tanti "troppe" del server) si butta la piu' vecchia, e la si restituisce.
    pub fn accoda(&mut self, s: Segnalazione, ora: i64) -> Result<Option<Segnalazione>, &'static str> {
        // anche quelle "nel futuro" (orologio spostato e poi corretto) si dimenticano
        self.fatte.retain(|(_, _, t)| *t <= ora && ora - t < STESSO_TESTO);
        if self.fatte.iter().any(|(tipo, _, t)| *tipo == s.tipo && ora - t < STESSO_TIPO) {
            return Err("stesso tipo da meno di 6 ore");
        }
        if self.fatte.iter().any(|(_, testo, _)| *testo == s.testo) {
            return Err("stesso problema da meno di un giorno");
        }
        if self.fatte.len() >= AL_GIORNO {
            return Err("troppe oggi");
        }
        let buttata = (self.in_attesa.len() >= CODA).then(|| self.in_attesa.remove(0));
        self.fatte.push((s.tipo, s.testo.clone(), ora));
        self.in_attesa.push(s);
        Ok(buttata)
    }
}

/// Come ha risposto il server.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Risposta {
    /// 200.
    Ricevuta,
    /// 429: troppe, si riprova al prossimo avvio.
    Troppe,
    /// 400 (o un altro 4xx): non la vuole, si lascia perdere.
    Rifiutata,
    /// Rete, timeout, server giu': si riprova al prossimo avvio.
    Rete,
}

/// Chi manda davvero il corpo JSON: il server vero, o uno finto nelle prove.
pub type Invio<'a> = &'a dyn Fn(&str) -> Risposta;

/// Il POST al server, con un tempo massimo.
fn invia_al_server(indirizzo: &str, corpo: &str) -> Risposta {
    let esito = reqwest::blocking::Client::builder()
        .timeout(ATTESA_RETE)
        .build()
        .and_then(|c| c.post(indirizzo).header("Content-Type", "application/json").body(corpo.to_string()).send());
    match esito.map(|r| r.status().as_u16()) {
        Ok(200..=299) => Risposta::Ricevuta,
        Ok(429) => Risposta::Troppe,
        Ok(400..=499) => Risposta::Rifiutata,
        _ => Risposta::Rete,
    }
}

/// Codici fiscali (anche omocodici), numeri di ricetta, date, orari, email, telefoni e cartelle dell'utente al
/// posto giusto: segnaposto. Meglio togliere un numero di troppo che lasciarne uno.
pub fn pulito(testo: &str) -> String {
    static REGOLE: OnceLock<Vec<(Regex, &'static str)>> = OnceLock::new();
    let regole = REGOLE.get_or_init(|| {
        let mesi = "gen(?:naio)?|feb(?:braio)?|mar(?:zo)?|apr(?:ile)?|mag(?:gio)?|giu(?:gno)?|lug(?:lio)?|\
                    ago(?:sto)?|set(?:tembre)?|ott(?:obre)?|nov(?:embre)?|dic(?:embre)?";
        let lunghi = "gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre";
        let cifra = "[0-9LMNPQRSTUV]";
        [
            (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}".to_string(), "<email>"),
            // senza confini di parola: anche "CF_RSSMRA85T10A562S"
            (format!("(?i)[A-Z]{{6}}{cifra}{{2}}[ABCDEHLMPRST]{cifra}{{2}}[A-Z]{cifra}{{3}}[A-Z]"), "<codice fiscale>"),
            (r"\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?".to_string(), "<data>"),
            (r"\d{1,2}[./-]\d{1,2}[./-]\d{4}".to_string(), "<data>"),
            (r"\d{1,2}/\d{1,2}(?:/\d{2,4})?".to_string(), "<data>"),
            (format!(r"(?i)\d{{1,2}}\s*(?:{mesi})\b\.?(?:\s*\d{{4}})?"), "<data>"),
            (format!(r"(?i)\b(?:{lunghi})\b(?:\s+\d{{4}})?"), "<data>"),
            // "202612010900": le chiavi delle date del portale
            (
                r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:(?:[01]\d|2[0-3])[0-5]\d)?".to_string(),
                "<data>",
            ),
            (r"\d{1,2}:\d{2}(?::\d{2})?".to_string(), "<ora>"),
            (r"\+?\d(?:[ .-]?\d){7,13}".to_string(), "<telefono>"),
        ]
        .into_iter()
        .map(|(r, s)| (Regex::new(&r).expect("espressione delle segnalazioni"), s))
        .collect()
    });
    let mut t = senza_percorsi(testo);
    // l'email prima di tutto (ha dentro di tutto), il codice fiscale prima del numero di ricetta, le date prima
    // dei telefoni (anche "2026-12-01" sono otto cifre)
    for (i, (r, segnaposto)) in regole.iter().enumerate() {
        t = r.replace_all(&t, *segnaposto).into_owned();
        if i == 1 {
            t = senza_ricette(&t);
        }
    }
    t
}

/// I numeri di ricetta: 15 lettere o cifre di fila (con almeno una cifra), anche in minuscolo o attaccati a "_".
fn senza_ricette(testo: &str) -> String {
    let mut fuori = String::with_capacity(testo.len());
    let mut parola = String::new();
    let chiudi = |parola: &mut String, fuori: &mut String| {
        if parola.len() == 15 && parola.chars().any(|c| c.is_ascii_digit()) {
            fuori.push_str("<ricetta>");
        } else {
            fuori.push_str(parola);
        }
        parola.clear();
    };
    for c in testo.chars() {
        if c.is_ascii_alphanumeric() {
            parola.push(c);
        } else {
            chiudi(&mut parola, &mut fuori);
            fuori.push(c);
        }
    }
    chiudi(&mut parola, &mut fuori);
    fuori
}

/// Le righe del registro con i secondi prima della segnalazione al posto di data e ora ("-3605s avvio").
fn righe_relative(righe: &[String], ora: i64) -> Vec<String> {
    righe
        .iter()
        .map(|r| {
            let quando = r.get(..19).and_then(|d| NaiveDateTime::parse_from_str(d, "%Y-%m-%d %H:%M:%S").ok());
            match quando.and_then(|q| Local.from_local_datetime(&q).earliest()) {
                Some(q) => format!("-{}s{}", (ora - q.timestamp()).max(0), &r[19..]),
                None => r.clone(),
            }
        })
        .collect()
}

/// Taglia a `massimo` byte, senza spezzare un carattere.
fn tagliato(s: &str, massimo: usize) -> &str {
    let mut fine = s.len().min(massimo);
    while !s.is_char_boundary(fine) {
        fine -= 1;
    }
    &s[..fine]
}

/// Chi e' l'app che segnala.
#[derive(Debug, Clone)]
pub struct Contesto {
    pub versione: String,
    /// "windows-x86_64".
    pub sistema: String,
    /// "nsis", "msi", "app"... o "sconosciuta".
    pub installazione: &'static str,
}

/// Il rapporto, gia' pulito e non piu' lungo di RAPPORTO. Di un panico solo dove, mai il messaggio. Se e' troppo
/// lungo si tolgono le righe del registro piu' vecchie.
pub fn rapporto(c: &Contesto, tipo: Tipo, testo: &str, dove: Option<&str>, registro: &[String], ora: i64) -> String {
    let mut testa = format!(
        "CUP Piemonte {}, {}, installazione {}\nTipo: {}\nChe cosa e' successo: {testo}\n",
        c.versione,
        c.sistema,
        c.installazione,
        tipo.nome()
    );
    if let Some(d) = dove {
        testa.push_str(&format!("Dove: {d}\n"));
    }
    let testa = format!("{}\n{REGISTRO}\n", pulito(&testa));
    // dalle piu' recenti, finche' ci stanno
    let mut spazio = RAPPORTO.saturating_sub(testa.len());
    let mut righe = vec![];
    for r in righe_relative(registro, ora).iter().rev().map(|r| pulito(r)) {
        if r.len() + 1 > spazio {
            break;
        }
        spazio -= r.len() + 1;
        righe.push(r);
    }
    righe.reverse();
    let r = format!("{testa}{}\n", righe.join("\n"));
    tagliato(&r, RAPPORTO).to_string()
}

const REGISTRO: &str = "Ultime righe del registro (secondi prima della segnalazione):";

/// Il corpo del POST, come lo vuole il server, non piu' grande di CORPO: in JSON barre rovesciate, virgolette e
/// caratteri di controllo crescono, quindi si misura dopo e, se serve, si tolgono le righe del registro piu' vecchie.
pub fn corpo(c: &Contesto, id: &str, s: &Segnalazione) -> String {
    let mut rapporto = tagliato(&s.rapporto, RAPPORTO).to_string();
    loop {
        let b = serde_json::json!({
            "v": 1,
            "app": c.versione,
            "sistema": c.sistema,
            "installazione": c.installazione,
            "id": id,
            "tipo": s.tipo.nome(),
            "testo": tagliato(&s.testo, TESTO),
            "rapporto": rapporto,
        })
        .to_string();
        if b.len() <= CORPO || rapporto.is_empty() {
            return b;
        }
        // la riga del registro piu' vecchia; senza righe (o senza registro) si taglia a meta'
        let inizio = rapporto.find(REGISTRO).map(|i| i + REGISTRO.len() + 1).filter(|i| *i < rapporto.len());
        rapporto = match inizio.and_then(|i| rapporto[i..].find('\n').map(|f| (i, i + f + 1))) {
            Some((i, f)) => format!("{}{}", &rapporto[..i], &rapporto[f..]),
            None => tagliato(&rapporto, rapporto.len() / 2).to_string(),
        };
    }
}

fn nuovo_id() -> String {
    let mut b = [0u8; 8];
    OsRng.fill_bytes(&mut b);
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn leggi<T: serde::de::DeserializeOwned + Default>(file: &Path) -> T {
    // un file rovinato non deve fermare nulla: si riparte da capo
    fs::read(file).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

/// Cosa mostra l'interfaccia.
#[derive(Serialize)]
pub struct Info {
    /// L'app e' stata compilata con l'indirizzo del server.
    pub disponibili: bool,
    pub accese: bool,
    /// "01/11/2026 14:05, aggiornamento".
    pub ultima: Option<String>,
}

pub struct Segnalazioni {
    file: PathBuf,
    file_panico: PathBuf,
    contesto: Contesto,
    registro: Registro,
    indirizzo: Option<&'static str>,
    memoria: Mutex<Memoria>,
    /// Lo stesso valore di `memoria.accese`, per il gancio del panico (che non prende lock).
    accese: AtomicBool,
    /// La rete non va o il server ne ha ricevute troppe: fino al prossimo avvio non si manda piu' nulla.
    fermo: AtomicBool,
    /// Un invio alla volta.
    lavoro: Mutex<()>,
    /// Controlli di fila finiti su una pagina del portale non riconosciuta.
    inattese: AtomicU32,
}

impl Segnalazioni {
    /// `cartella` e' quella dei dati, `registro` quella del registro. Le segnalazioni di un panico dell'avvio prima
    /// passano in coda (se sono ancora accese).
    pub fn new(cartella: &Path, registro: &Path, contesto: Contesto, indirizzo: Option<&'static str>) -> Self {
        let file = cartella.join(FILE);
        let mut memoria: Memoria = leggi(&file);
        if memoria.id.len() != 16 {
            memoria.id = nuovo_id();
        }
        let s = Segnalazioni {
            file,
            file_panico: cartella.join(FILE_PANICO),
            contesto,
            registro: Registro::new(registro),
            indirizzo,
            accese: AtomicBool::new(memoria.accese && indirizzo.is_some()),
            memoria: Mutex::new(memoria),
            fermo: AtomicBool::new(false),
            lavoro: Mutex::new(()),
            inattese: AtomicU32::new(0),
        };
        let panici: Vec<Segnalazione> = leggi(&s.file_panico);
        if s.file_panico.exists() {
            let _ = fs::remove_file(&s.file_panico);
        }
        for p in panici {
            s.accoda(p);
        }
        s
    }

    pub fn attive(&self) -> bool {
        self.accese.load(Ordering::SeqCst)
    }

    pub fn memoria(&self) -> Memoria {
        blocca(&self.memoria).clone()
    }

    /// Cambia lo stato e lo salva.
    fn cambia<T>(&self, f: impl FnOnce(&mut Memoria) -> T) -> Result<T, String> {
        let mut m = blocca(&self.memoria);
        let r = f(&mut m);
        let b = serde_json::to_vec_pretty(&*m).map_err(|e| e.to_string())?;
        crate::archivio::scrivi_privato(&self.file, &b)?;
        Ok(r)
    }

    /// Accende o spegne. Spente, quelle in attesa si buttano: non parte piu' nulla.
    pub fn imposta(&self, si: bool) -> Result<(), String> {
        if self.indirizzo.is_none() {
            return Err("Le segnalazioni non sono disponibili in questa versione dell'app.".into());
        }
        // spente: prima il segnale (niente piu' code ne' invii), poi la coda si svuota
        if !si {
            self.accese.store(false, Ordering::SeqCst);
        }
        self.cambia(|m| {
            m.accese = si;
            if !si {
                m.in_attesa.clear();
            }
        })?;
        if !si {
            let _ = fs::remove_file(&self.file_panico);
        }
        self.accese.store(si, Ordering::SeqCst);
        Ok(())
    }

    pub fn info(&self) -> Info {
        let m = self.memoria();
        let ultima = m.ultima.map(|(ts, tipo)| {
            let t = Local.timestamp_opt(ts, 0).single().unwrap_or_else(Local::now);
            format!("{}, {}", t.format("%d/%m/%Y %H:%M"), tipo.nome())
        });
        Info { disponibili: self.indirizzo.is_some(), accese: self.attive(), ultima }
    }

    /// Il rapporto di adesso: le ultime righe del registro, pulite.
    fn rapporto(&self, tipo: Tipo, testo: &str, dove: Option<&str>) -> String {
        rapporto(&self.contesto, tipo, testo, dove, &self.registro.ultime(RIGHE), adesso())
    }

    fn accoda(&self, s: Segnalazione) -> bool {
        let tipo = s.tipo;
        // accese si guarda sotto il lock: `imposta(false)` le spegne prima di svuotare la coda
        match self.cambia(|m| if self.attive() && m.accese { Some(m.accoda(s, adesso())) } else { None }) {
            Ok(None) => false,
            Ok(Some(Ok(buttata))) => {
                if let Some(b) = buttata {
                    self.registro.scrivi(&format!(
                        "segnalazioni: scartata segnalazione {} (troppe in attesa, era la piu' vecchia)",
                        b.tipo.nome()
                    ));
                }
                true
            }
            Ok(Some(Err(perche))) => {
                self.registro.scrivi(&format!("segnalazioni: scartata segnalazione {} ({perche})", tipo.nome()));
                false
            }
            Err(e) => {
                self.registro.scrivi(&format!("segnalazioni: stato non salvato: {}", senza_percorsi(&e)));
                false
            }
        }
    }

    /// Prepara la segnalazione e la mette in coda (non la manda: vedi `invia`). False se spente o fuori dai limiti.
    pub fn segnala(&self, tipo: Tipo, testo: &str) -> bool {
        if !self.attive() {
            return false;
        }
        let rapporto = self.rapporto(tipo, testo, None);
        self.accoda(Segnalazione { tipo, testo: testo.to_string(), rapporto, ts: adesso(), tentativi: 0 })
    }

    /// L'esito di un controllo: true se e' la seconda pagina non riconosciuta di fila (allora si segnala).
    pub fn controllo(&self, inattesa: bool) -> bool {
        if !inattesa {
            self.inattese.store(0, Ordering::SeqCst);
            return false;
        }
        if self.inattese.fetch_add(1, Ordering::SeqCst) + 1 >= 2 {
            self.inattese.store(0, Ordering::SeqCst);
            return true;
        }
        false
    }

    /// Dal gancio del panico: niente rete e nessun lock dello stato, solo un file accanto alle impostazioni.
    fn scrivi_panico(&self, dove: Option<&str>, principale: bool) {
        let testo = if principale { CHIUSA } else { INTERNO };
        let rapporto = self.rapporto(Tipo::Panico, testo, dove);
        let mut panici: Vec<Segnalazione> = leggi(&self.file_panico);
        // come la coda: piena, si butta il piu' vecchio
        if panici.len() >= CODA {
            panici.remove(0);
        }
        panici.push(Segnalazione { tipo: Tipo::Panico, testo: testo.into(), rapporto, ts: adesso(), tentativi: 0 });
        if let Ok(b) = serde_json::to_vec(&panici) {
            let _ = crate::archivio::scrivi_privato(&self.file_panico, &b);
        }
    }

    /// Manda quelle in attesa, una alla volta, con `invio`. Si ferma alla prima che non parte per la rete o perche'
    /// il server ne ha ricevute troppe: si riprova al prossimo avvio.
    pub fn invia(&self, invio: Invio) {
        let _uno = blocca(&self.lavoro);
        while self.attive() && !self.fermo.load(Ordering::SeqCst) {
            let (s, id) = {
                let m = blocca(&self.memoria);
                match m.in_attesa.first() {
                    Some(s) => (s.clone(), m.id.clone()),
                    None => return,
                }
            };
            let corpo = corpo(&self.contesto, &id, &s);
            // spente mentre si preparava: non parte
            if !self.attive() {
                return;
            }
            let risposta = invio(&corpo);
            if matches!(risposta, Risposta::Troppe | Risposta::Rete) {
                self.fermo.store(true, Ordering::SeqCst);
            }
            let tipo = s.tipo.nome();
            let riga = self.cambia(|m| {
                // spente nel frattempo (coda vuota) o buttata perche' la coda era piena
                if m.in_attesa.first() != Some(&s) {
                    return None;
                }
                Some(match risposta {
                    Risposta::Ricevuta => {
                        m.in_attesa.remove(0);
                        m.ultima = Some((adesso(), s.tipo));
                        format!("segnalazioni: inviata segnalazione {tipo}")
                    }
                    Risposta::Rifiutata => {
                        m.in_attesa.remove(0);
                        format!("segnalazioni: segnalazione {tipo} rifiutata dal server, scartata")
                    }
                    Risposta::Troppe => {
                        "segnalazioni: il server ne ha ricevute troppe, riprovo al prossimo avvio".to_string()
                    }
                    Risposta::Rete => {
                        m.in_attesa[0].tentativi += 1;
                        if m.in_attesa[0].tentativi >= TENTATIVI {
                            m.in_attesa.remove(0);
                            format!("segnalazioni: segnalazione {tipo} non inviata {TENTATIVI} volte, scartata")
                        } else {
                            format!("segnalazioni: segnalazione {tipo} non inviata (rete), riprovo al prossimo avvio")
                        }
                    }
                })
            });
            match riga {
                Ok(Some(r)) => self.registro.scrivi(&r),
                Ok(None) => {}
                Err(e) => {
                    self.registro.scrivi(&format!("segnalazioni: stato non salvato: {}", senza_percorsi(&e)));
                    return;
                }
            }
        }
    }

    /// Manda quelle in attesa al server vero, in un thread a parte.
    fn invia_dopo(self: &Arc<Self>, attesa: Duration) {
        let Some(indirizzo) = self.indirizzo else { return };
        let s = self.clone();
        thread::spawn(move || {
            thread::sleep(attesa);
            let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                s.invia(&|corpo| invia_al_server(indirizzo, corpo))
            }));
        });
    }
}

static SEGNALAZIONI: OnceLock<Arc<Segnalazioni>> = OnceLock::new();
/// Un panico alla volta nel gancio (e niente se il gancio stesso va in panico).
static PANICO: Mutex<()> = Mutex::new(());

/// Le segnalazioni dell'app, se partite.
pub fn globali() -> Option<&'static Arc<Segnalazioni>> {
    SEGNALAZIONI.get()
}

/// All'avvio: da qui le segnalazioni valgono per tutta l'app, con il gancio del panico. Quelle rimaste in attesa
/// partono tra poco.
pub fn avvia(s: Segnalazioni) {
    s.registro.scrivi(match (s.indirizzo, s.attive()) {
        (None, _) => "segnalazioni: non disponibili in questa versione",
        (_, true) => "segnalazioni: accese",
        _ => "segnalazioni: spente",
    });
    let s = Arc::new(s);
    if SEGNALAZIONI.set(s.clone()).is_err() {
        return;
    }
    let prima = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        al_panico(info);
        prima(info);
    }));
    s.invia_dopo(PRIMO);
}

fn al_panico(info: &std::panic::PanicHookInfo<'_>) {
    let Some(s) = globali() else { return };
    if !s.attive() {
        return;
    }
    let Ok(_uno) = PANICO.try_lock() else { return };
    // il messaggio del panico no (puo' contenere dati), la pila nemmeno (vedi sopra)
    let dove = info.location().map(|l| format!("{} riga {}", l.file(), l.line()));
    s.scrivi_panico(dove.as_deref(), thread::current().name() == Some("main"));
}

/// Segnala un problema, se le segnalazioni sono accese: prepara e manda in un thread a parte, non blocca mai.
pub fn segnala(tipo: Tipo, testo: &str) {
    let Some(s) = globali() else { return };
    if !s.attive() {
        return;
    }
    let (s, testo) = (s.clone(), testo.to_string());
    thread::spawn(move || {
        if s.segnala(tipo, &testo) {
            s.invia_dopo(Duration::ZERO);
        }
    });
}

/// L'esito di un controllo: alla seconda pagina del portale non riconosciuta di fila, una segnalazione.
pub fn controllo(inattesa: bool) {
    if globali().is_some_and(|s| s.controllo(inattesa)) {
        segnala(Tipo::Portale, PORTALE);
    }
}

#[cfg(test)]
mod test {
    use std::cell::RefCell;

    use super::*;

    const ORA: i64 = 1_800_000_000;

    fn contesto() -> Contesto {
        Contesto { versione: "0.3.1".into(), sistema: "windows-x86_64".into(), installazione: "nsis" }
    }

    fn segnalazione(tipo: Tipo, testo: &str) -> Segnalazione {
        Segnalazione { tipo, testo: testo.into(), rapporto: "r".into(), ts: ORA, tentativi: 0 }
    }

    fn cartella(nome: &str) -> PathBuf {
        let c = std::env::temp_dir().join(format!("cup-test-segn-{nome}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&c);
        fs::create_dir_all(&c).unwrap();
        c
    }

    fn accese(c: &Path) -> Segnalazioni {
        let s = Segnalazioni::new(c, c, contesto(), Some("http://prova.invalid/segnalazioni"));
        s.imposta(true).unwrap();
        s
    }

    #[test]
    fn pulito_toglie_codici_ricette_date_e_contatti() {
        let casi = [
            ("cf RSSMRA85T10A562S fine", "cf <codice fiscale> fine"),
            // omocodico (cifre sostituite da lettere), minuscolo, attaccato a "_"
            ("RSSMRA85T10A56NS", "<codice fiscale>"),
            ("rssmraurtmlarsnl", "<codice fiscale>"),
            ("x:CF_RSSMRA85T10A562S_b", "x:CF_<codice fiscale>_b"),
            ("ricetta 010A00000000001.", "ricetta <ricetta>."),
            ("riga_010a00000000001_b", "riga_<ricetta>_b"),
            ("il 03/12 alle 9:30", "il <data> alle <ora>"),
            ("mar 01/12/2026 ore 09:00", "mar <data> ore <ora>"),
            ("01-12-2026 e 2026-12-01T09:00", "<data> e <data>"),
            ("chiave 202612010900|X e x_20261201", "chiave <data>|X e x_<data>"),
            ("il 3 nov 2026, il 12 dicembre, il 5 mag.", "il <data>, il <data>, il <data>"),
            ("a novembre 2026", "a <data>"),
            ("scrivi a mario.rossi@example.com", "scrivi a <email>"),
            // i contatti di una ricetta, come li salva l'app e come li scrive l'utente
            ("email mario.rossi+cup@poste-it.example.it, cellulare 3471234567", "email <email>, cellulare <telefono>"),
            ("cell +39 347 123 4567 o 347-1234567 o 0039 347.1234567", "cell <telefono> o <telefono> o <telefono>"),
            ("tel +39 333 1234567 o 011.5551234", "tel <telefono> o <telefono>"),
        ];
        for (prima, dopo) in casi {
            assert_eq!(pulito(prima), dopo, "{prima}");
        }
        // restano le cose che servono: versioni, numeri delle ricette nell'app, durate, tipi di errore
        let resta = "ricetta 3: 12 date offerte, 2 buone, 41 s; versione 0.3.10; portale sovraccarico (HTTP 503)";
        assert_eq!(pulito(resta), resta);
        // la cartella dell'utente (spesso ha il suo nome)
        let casa = std::env::var("USERPROFILE").or_else(|_| std::env::var("HOME")).unwrap();
        assert!(!pulito(&format!("file {casa}{}x non trovato", std::path::MAIN_SEPARATOR)).contains(&casa));
    }

    #[test]
    fn limiti_per_tipo_per_frase_e_al_giorno() {
        let mut m = Memoria::default();
        m.accoda(segnalazione(Tipo::Portale, "a"), ORA).unwrap();
        // lo stesso tipo: non prima di 6 ore
        assert!(m.accoda(segnalazione(Tipo::Portale, "b"), ORA + STESSO_TIPO - 1).is_err());
        m.accoda(segnalazione(Tipo::Portale, "b"), ORA + STESSO_TIPO).unwrap();
        // la stessa frase: non prima di un giorno, anche con un altro tipo
        assert!(m.accoda(segnalazione(Tipo::Errore, "a"), ORA + STESSO_TIPO).is_err());
        m.accoda(segnalazione(Tipo::Errore, "a"), ORA + STESSO_TESTO).unwrap();
        // al massimo CODA in attesa
        let mut m = Memoria::default();
        for i in 0..CODA {
            m.accoda(segnalazione(Tipo::Errore, &i.to_string()), ORA + i as i64 * STESSO_TIPO).unwrap();
        }
        // con la coda piena si butta la piu' vecchia, non la nuova
        let buttata = m.accoda(segnalazione(Tipo::Avvio, "x"), ORA).unwrap().unwrap();
        assert_eq!(buttata.testo, "0");
        assert_eq!((m.in_attesa.len(), m.in_attesa.last().unwrap().testo.as_str()), (CODA, "x"));
        // al massimo AL_GIORNO in 24 ore, anche se partite
        let mut m = Memoria::default();
        let tipi = [Tipo::Panico, Tipo::Avvio, Tipo::Aggiornamento, Tipo::Portale, Tipo::Errore];
        for i in 0..AL_GIORNO {
            m.in_attesa.clear();
            m.accoda(segnalazione(tipi[i % 5], &i.to_string()), ORA + (i / 5) as i64 * STESSO_TIPO).unwrap();
        }
        m.in_attesa.clear();
        assert_eq!(m.accoda(segnalazione(Tipo::Avvio, "z"), ORA + 2 * STESSO_TIPO), Err("troppe oggi"));
        assert!(m.accoda(segnalazione(Tipo::Avvio, "z"), ORA + STESSO_TESTO + 2 * STESSO_TIPO).is_ok());
        // l'orologio torna indietro: le segnalazioni "nel futuro" non bloccano
        let mut m = Memoria::default();
        m.accoda(segnalazione(Tipo::Avvio, "a"), ORA).unwrap();
        assert!(m.accoda(segnalazione(Tipo::Avvio, "a"), ORA - 3600).is_ok());
    }

    #[test]
    fn rapporto_senza_messaggio_del_panico_e_senza_dati() {
        let registro = vec![
            "2026-11-01 14:00:00 avvio, versione 0.3.1".to_string(),
            "2026-11-01 14:05:03 ricetta 1: risposta inattesa (Il portale ha risposto) dopo 3 s".to_string(),
            "riga senza ora del 03/12".to_string(),
        ];
        let ora = Local.with_ymd_and_hms(2026, 11, 1, 14, 10, 0).unwrap().timestamp();
        let r = rapporto(&contesto(), Tipo::Panico, CHIUSA, Some("src/controlli.rs riga 42"), &registro, ora);
        assert!(r.starts_with("CUP Piemonte 0.3.1, windows-x86_64, installazione nsis\nTipo: panico\n"), "{r}");
        assert!(r.contains(CHIUSA) && r.contains("Dove: src/controlli.rs riga 42") && !r.contains("Pila"));
        assert!(r.contains("\n-600s avvio, versione 0.3.1\n-297s ricetta 1: risposta inattesa"), "{r}");
        assert!(r.contains("riga senza ora del <data>") && !r.contains("2026-11-01"));
        // il messaggio del panico e la pila non arrivano mai qui: il gancio passa solo dove
        let c = cartella("panico");
        let s = accese(&c);
        s.registro.scrivi("ricetta 2: controllo");
        s.scrivi_panico(Some("src/x.rs riga 1"), true);
        let panici: Vec<Segnalazione> = leggi(&c.join(FILE_PANICO));
        assert_eq!(panici.len(), 1);
        assert!(panici[0].rapporto.contains("ricetta 2: controllo") && panici[0].testo == CHIUSA);
        assert!(panici[0].rapporto.contains("Dove: src/x.rs riga 1\n\n"));
        // troppo lungo: si tolgono le righe vecchie, il corpo resta sotto i 32 KB
        let lunghe: Vec<String> = (0..2000).map(|i| format!("riga {i} {}", "è".repeat(40))).collect();
        let r = rapporto(&contesto(), Tipo::Errore, INCERTO, None, &lunghe, ora);
        assert!(r.len() <= RAPPORTO && r.contains("riga 1999") && !r.contains("riga 0 "));
        let b = corpo(&contesto(), "0123456789abcdef", &segnalazione(Tipo::Errore, &"x".repeat(500)));
        assert!(b.len() <= 32 * 1024);
        let v: serde_json::Value = serde_json::from_str(&b).unwrap();
        assert_eq!(
            (v["v"].as_i64(), v["tipo"].as_str(), v["installazione"].as_str()),
            (Some(1), Some("errore"), Some("nsis"))
        );
        assert_eq!(v["testo"].as_str().unwrap().len(), TESTO);
        fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn coda_salvata_invii_e_tentativi() {
        let c = cartella("coda");
        let s = accese(&c);
        let id = s.memoria().id.clone();
        assert!(id.len() == 16 && id.chars().all(|x| x.is_ascii_hexdigit()));
        assert!(s.segnala(Tipo::Aggiornamento, &aggiornamento_non_riuscito("0.3.2")));
        assert!(!s.segnala(Tipo::Aggiornamento, &aggiornamento_non_riuscito("0.3.2"))); // gia' fatta
        assert!(s.segnala(Tipo::Errore, INCERTO));
        // la rete non va: resta in attesa, e fino al prossimo avvio non si riprova
        let corpi = RefCell::new(vec![]);
        let rete = |b: &str| {
            corpi.borrow_mut().push(b.to_string());
            Risposta::Rete
        };
        s.invia(&rete);
        s.invia(&rete);
        assert_eq!(corpi.borrow().len(), 1);
        let v: serde_json::Value = serde_json::from_str(&corpi.borrow()[0]).unwrap();
        assert_eq!((v["id"].as_str(), v["tipo"].as_str()), (Some(id.as_str()), Some("aggiornamento")));
        assert_eq!(v["testo"], "Aggiornamento alla versione 0.3.2 non riuscito");
        drop(s);
        // al riavvio la coda c'e' ancora (stesso id): alla terza volta si lascia perdere
        for _ in 2..=TENTATIVI {
            let s = Segnalazioni::new(&c, &c, contesto(), Some("http://prova.invalid"));
            assert_eq!((s.memoria().id, s.memoria().in_attesa.len()), (id.clone(), 2));
            s.invia(&|_| Risposta::Rete);
        }
        let s = Segnalazioni::new(&c, &c, contesto(), Some("http://prova.invalid"));
        let m = s.memoria();
        assert_eq!((m.in_attesa.len(), m.in_attesa[0].tipo), (1, Tipo::Errore));
        // "troppe": si ferma senza contare un tentativo
        s.invia(&|_| Risposta::Troppe);
        assert_eq!(s.memoria().in_attesa[0].tentativi, 0);
        // ricevuta: l'ultima inviata si vede nelle impostazioni
        let s = Segnalazioni::new(&c, &c, contesto(), Some("http://prova.invalid"));
        assert!(s.info().ultima.is_none());
        s.invia(&|_| Risposta::Ricevuta);
        let m = s.memoria();
        assert!(m.in_attesa.is_empty() && m.ultima.map(|u| u.1) == Some(Tipo::Errore));
        assert!(s.info().ultima.unwrap().ends_with(", errore"));
        let righe = s.registro.ultime(20);
        assert!(righe.iter().any(|r| r.ends_with("segnalazioni: inviata segnalazione errore")), "{righe:?}");
        assert!(righe
            .iter()
            .any(|r| r.ends_with("segnalazioni: segnalazione aggiornamento non inviata 3 volte, scartata")));
        // rifiutata: si butta
        assert!(s.segnala(Tipo::Avvio, NON_PARTE));
        s.invia(&|_| Risposta::Rifiutata);
        assert!(s.memoria().in_attesa.is_empty());
        // spente: la coda si svuota e non parte piu' nulla
        assert!(s.segnala(Tipo::Portale, PORTALE));
        s.imposta(false).unwrap();
        assert!(s.memoria().in_attesa.is_empty() && !s.segnala(Tipo::Panico, CHIUSA));
        s.invia(&|_| panic!("spente: niente invii"));
        fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn panico_scritto_su_file_e_in_coda_al_riavvio() {
        let c = cartella("riavvio");
        let s = accese(&c);
        for _ in 0..CODA + 2 {
            s.scrivi_panico(None, false);
        }
        assert_eq!(leggi::<Vec<Segnalazione>>(&c.join(FILE_PANICO)).len(), CODA);
        drop(s);
        let s = Segnalazioni::new(&c, &c, contesto(), Some("http://prova.invalid"));
        // il file si consuma; i limiti valgono anche qui: un panico solo (stesso tipo)
        assert!(!c.join(FILE_PANICO).exists());
        let m = s.memoria();
        assert_eq!((m.in_attesa.len(), m.in_attesa[0].testo.as_str()), (1, INTERNO));
        fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn senza_indirizzo_non_si_accendono_e_non_parte_nulla() {
        let c = cartella("senza");
        // anche con un file che le dice accese (per esempio scritto da un'altra versione)
        let m = Memoria { accese: true, ..Default::default() };
        fs::write(c.join(FILE), serde_json::to_vec(&m).unwrap()).unwrap();
        let s = Segnalazioni::new(&c, &c, contesto(), None);
        assert!(!s.attive() && s.imposta(true).is_err());
        let i = s.info();
        assert!(!i.disponibili && !i.accese);
        assert!(!s.segnala(Tipo::Avvio, NON_PARTE));
        s.invia(&|_| panic!("senza indirizzo: niente invii"));
        // e spente per chi non le ha accese
        let c2 = cartella("spente");
        let s = Segnalazioni::new(&c2, &c2, contesto(), Some("http://prova.invalid"));
        assert!(!s.attive() && !s.segnala(Tipo::Avvio, NON_PARTE));
        fs::remove_dir_all(&c).unwrap();
        fs::remove_dir_all(&c2).unwrap();
    }

    #[test]
    fn corpo_sotto_i_32_kb_anche_con_tante_barre_rovesciate() {
        // percorsi di Windows e virgolette raddoppiano in JSON: 24.000 byte di rapporto non bastano a stare sotto
        let righe: Vec<String> = (0..400).map(|i| format!("riga {i} C:\\{}", "\\\"".repeat(30))).collect();
        let r = rapporto(&contesto(), Tipo::Avvio, NON_PARTE, None, &righe, ORA);
        assert!(r.len() > CORPO / 2 && serde_json::to_string(&r).unwrap().len() > CORPO);
        let s = Segnalazione { tipo: Tipo::Avvio, testo: NON_PARTE.into(), rapporto: r, ts: ORA, tentativi: 0 };
        let b = corpo(&contesto(), "0123456789abcdef", &s);
        assert!(b.len() <= CORPO, "{}", b.len());
        // si tolgono le righe piu' vecchie: la testa e le piu' recenti restano
        let v: serde_json::Value = serde_json::from_str(&b).unwrap();
        let rap = v["rapporto"].as_str().unwrap();
        assert!(rap.starts_with("CUP Piemonte 0.3.1") && rap.contains("riga 399 ") && !rap.contains("riga 0 "));
        // anche un rapporto senza righe del registro (tutto controlli) si accorcia
        let s = Segnalazione { rapporto: "\u{1}".repeat(RAPPORTO), ..s };
        assert!(corpo(&contesto(), "0123456789abcdef", &s).len() <= CORPO);
    }

    #[test]
    fn solo_indirizzi_https() {
        assert_eq!(valido(Some(" https://esempio.invalid/x ")), Some("https://esempio.invalid/x"));
        assert_eq!(valido(Some("http://esempio.invalid/x")), None);
        assert_eq!(valido(Some("https://")), None);
        assert_eq!(valido(Some("")), None);
        assert_eq!(valido(None), None);
    }

    #[test]
    fn spente_mentre_si_invia_non_parte_nulla() {
        let c = cartella("corsa");
        let s = accese(&c);
        assert!(s.segnala(Tipo::Avvio, NON_PARTE) && s.segnala(Tipo::Errore, INCERTO));
        // la prima parte, e durante l'invio l'utente le spegne: la seconda non parte
        let invii = RefCell::new(0);
        s.invia(&|_| {
            *invii.borrow_mut() += 1;
            s.imposta(false).unwrap();
            Risposta::Ricevuta
        });
        assert_eq!(*invii.borrow(), 1);
        assert!(s.memoria().in_attesa.is_empty() && !s.memoria().accese);
        fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn pagina_non_riconosciuta_due_volte_di_fila() {
        let c = cartella("portale");
        let s = accese(&c);
        assert!(!s.controllo(true));
        assert!(!s.controllo(false)); // un controllo andato bene in mezzo azzera
        assert!(!s.controllo(true));
        assert!(s.controllo(true));
        assert!(!s.controllo(true)); // si ricomincia a contare
        fs::remove_dir_all(&c).unwrap();
    }
}
