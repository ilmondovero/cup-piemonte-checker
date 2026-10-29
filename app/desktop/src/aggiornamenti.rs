//! Aggiornamenti automatici dell'app, fatti in modo da non lasciare mai l'utente con un'app rotta o con i dati persi.
//!
//! - Dove: due canali indipendenti (una release di GitHub e la pagina GitHub Pages, vedi `plugins > updater` in
//!   tauri.conf.json). Se il primo non risponde si prova il secondo.
//! - Firma: ogni pacchetto e' firmato e la firma comprende la versione (`requireSignedVersion`). Il plugin installa
//!   solo un pacchetto firmato con la chiave scritta dentro l'app, e mai una versione piu' vecchia.
//! - Quando si controlla: qualche minuto dopo l'avvio, poi ogni 6 ore; dopo un errore prima (da 15 minuti in su).
//! - Rodaggio: una versione nuova si installa da sola solo 48 ore dopo la pubblicazione, cosi' se si rivela
//!   difettosa la si ritira prima che arrivi a tutti; il canale puo' segnarla "urgente". Subito prima di installare
//!   il canale si rilegge: una versione ritirata nel frattempo non si installa. "Aggiorna" non aspetta.
//! - Mai durante un controllo, uno spostamento, una prenotazione o una disdetta: si parte solo con niente in corso, e
//!   da li' non ne parte piu' nessuno (`Stato::chiudi_per_aggiornare`), fino al riavvio o a un errore.
//! - Prima di installare, una copia dell'archivio delle ricette (vedi `Archivio::copia`).
//! - Al riavvio si controlla di essere davvero la versione nuova. Un'installazione non riuscita si riprova dopo
//!   un'ora; dopo TENTATIVI l'app smette di riprovarla da sola e lo dice.
//! - Da sola solo dove il sistema non chiede una password: installer .exe (per il solo utente), AppImage, app per
//!   macOS, e solo se la cartella dell'app si puo' scrivere. Con .msi, .deb e .rpm (o un'app di macOS installata da
//!   un amministratore) serve la password: si installa solo toccando "Aggiorna".
//! - Se una versione non riesce piu' ad aprire le ricette, all'avvio cerca lo stesso una versione nuova e la installa
//!   (`soccorso`): una versione difettosa si corregge anche da lontano.
//!
//! Nel registro vanno solo versioni ed errori del canale, senza i percorsi della cartella dell'utente: mai dati delle
//! ricette.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use chrono::{Local, TimeZone};
use serde::{Deserialize, Serialize};
use tauri::utils::config::BundleType;
use tauri::AppHandle;
use tauri_plugin_updater::{Update, UpdaterExt};

use crate::controlli::{adesso, blocca, Stato};
use crate::registro::Registro;

const FILE: &str = "aggiornamenti.json";
/// Tra due controlli del canale.
pub const OGNI: i64 = 6 * 3600;
/// Dopo un controllo non riuscito: si riprova dopo 15 minuti, poi 30, 60... fino a OGNI.
pub const RIPROVA: i64 = 15 * 60;
/// Da quando una versione e' pubblicata a quando si installa da sola.
pub const RODAGGIO: i64 = 48 * 3600;
/// Installazioni non riuscite della stessa versione prima di smettere di riprovarla da sola.
pub const TENTATIVI: u32 = 2;
/// Dopo un'installazione non riuscita (anche solo il download), prima di riprovare da sola.
pub const RIPROVA_INSTALLAZIONE: i64 = 3600;
/// Subito prima di un'installazione da sola, il canale si rilegge se l'ultimo controllo e' piu' vecchio di cosi'.
const RILEGGI: i64 = 30 * 60;
/// Il primo controllo dopo l'avvio: non subito, l'avvio deve restare leggero.
const PRIMO: Duration = Duration::from_secs(3 * 60);
/// Ogni quanto si guarda se e' ora di controllare o di installare.
const PASSO: Duration = Duration::from_secs(5 * 60);
const ATTESA_RETE: Duration = Duration::from_secs(120);
/// Il download intero (decine di MB): una connessione piantata non deve tenere fermo tutto.
const ATTESA_DOWNLOAD: Duration = Duration::from_secs(15 * 60);

/// Come si aggiorna questa installazione.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Modo {
    /// Da sola, senza chiedere nulla: installer .exe per l'utente, AppImage, app per macOS.
    Automatico,
    /// Il sistema chiede la password di amministratore (.msi, .deb, .rpm): solo quando lo chiede l'utente.
    ARichiesta,
    /// Non si sa da dove viene (per esempio avviata da `cargo run`): niente aggiornamenti.
    NonDisponibile,
}

/// Il modo per tipo di installazione; `scrivibile` dice se l'utente puo' sostituire l'app senza password.
pub fn modo(installata_con: Option<&BundleType>, scrivibile: bool) -> Modo {
    match installata_con {
        Some(BundleType::Nsis | BundleType::AppImage | BundleType::App | BundleType::Dmg) if scrivibile => {
            Modo::Automatico
        }
        Some(_) => Modo::ARichiesta,
        None => Modo::NonDisponibile,
    }
}

pub fn nome_installazione(installata_con: Option<&BundleType>) -> &'static str {
    match installata_con {
        Some(BundleType::Nsis) => "nsis",
        Some(BundleType::Msi) => "msi",
        Some(BundleType::AppImage) => "appimage",
        Some(BundleType::Deb) => "deb",
        Some(BundleType::Rpm) => "rpm",
        Some(BundleType::App | BundleType::Dmg) => "app",
        None => "sconosciuta",
    }
}

/// La cartella che l'aggiornamento deve poter scrivere: quella dell'AppImage, quella che contiene "CUP Piemonte.app",
/// o quella dell'eseguibile.
fn cartella_dell_app(installata_con: Option<&BundleType>) -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    match installata_con {
        Some(BundleType::AppImage) => PathBuf::from(std::env::var_os("APPIMAGE")?).parent().map(Path::to_path_buf),
        Some(BundleType::App | BundleType::Dmg) => {
            let app = exe.ancestors().find(|p| p.extension().is_some_and(|e| e == "app"))?;
            app.parent().map(Path::to_path_buf)
        }
        _ => exe.parent().map(Path::to_path_buf),
    }
}

fn scrivibile(cartella: &Path) -> bool {
    let prova = cartella.join(format!(".cup-prova-scrittura-{}", std::process::id()));
    let ok = fs::write(&prova, b"").is_ok();
    let _ = fs::remove_file(&prova);
    ok
}

/// macOS sostituisce "CUP Piemonte.app" spostandolo: serve poter scrivere anche il bundle, non solo la cartella che
/// lo contiene. Un'app trascinata in Applicazioni da un altro utente non e' nostra: il sistema chiederebbe la
/// password. Si guarda il proprietario (dentro il bundle non si scrive nulla: invaliderebbe la firma).
#[cfg(target_os = "macos")]
fn bundle_nostro() -> bool {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let Some(app) = std::env::current_exe()
        .ok()
        .and_then(|e| e.ancestors().find(|p| p.extension().is_some_and(|x| x == "app")).map(Path::to_path_buf))
    else {
        return false;
    };
    // il nostro uid: quello di un file appena creato
    let prova = std::env::temp_dir().join(format!(".cup-uid-{}", std::process::id()));
    let io = fs::write(&prova, b"").and_then(|_| fs::metadata(&prova)).map(|m| m.uid());
    let _ = fs::remove_file(&prova);
    match (io, fs::metadata(&app)) {
        (Ok(io), Ok(m)) => m.uid() == io && m.permissions().mode() & 0o200 != 0,
        _ => false,
    }
}

#[cfg(not(target_os = "macos"))]
fn bundle_nostro() -> bool {
    true
}

/// Un testo senza i percorsi della cartella dell'utente (che spesso ha il suo nome): il registro si incolla nelle
/// Issues.
pub fn senza_percorsi(testo: &str) -> String {
    let mut cartelle: Vec<String> = ["LOCALAPPDATA", "APPDATA", "TEMP", "TMPDIR", "USERPROFILE", "HOME"]
        .iter()
        .filter_map(|v| std::env::var(v).ok())
        .filter(|p| p.len() > 3)
        .flat_map(|p| [p.clone(), p.replace('\\', "/"), p.replace('\\', "\\\\")])
        .collect();
    cartelle.sort_by_key(|p| std::cmp::Reverse(p.len())); // prima le piu' lunghe (stanno dentro la cartella utente)
    cartelle.into_iter().fold(testo.to_string(), |t, p| t.replace(&p, "~"))
}

fn piu_nuova(a: &str, di: &str) -> bool {
    match (semver::Version::parse(a), semver::Version::parse(di)) {
        (Ok(a), Ok(di)) => a > di,
        _ => a != di,
    }
}

/// Una versione nuova annunciata dal canale.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Disponibile {
    pub versione: String,
    /// Quando e' stata pubblicata (o, se il canale non lo dice, quando l'app l'ha vista la prima volta).
    pub pubblicata: i64,
    /// Il canale chiede di non aspettare il rodaggio.
    #[serde(default)]
    pub urgente: bool,
}

/// Un'installazione partita: al riavvio dice se e' andata.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Installazione {
    pub da: String,
    pub a: String,
    pub ts: i64,
    /// Partita da sola (al riavvio la finestra resta nascosta) o chiesta dall'utente.
    pub automatica: bool,
}

/// Cosa l'app ricorda degli aggiornamenti, in chiaro (nessun dato personale) in `aggiornamenti.json`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Canale {
    /// L'utente ha spento gli aggiornamenti automatici.
    pub spenti: bool,
    pub ultimo_controllo: i64,
    /// Controlli non riusciti di fila.
    pub errori: u32,
    pub ultimo_errore: Option<String>,
    pub disponibile: Option<Disponibile>,
    pub installazione: Option<Installazione>,
    /// Installazioni non riuscite: versione e quante volte.
    pub fallite: Option<(String, u32)>,
    /// Dopo un'installazione non riuscita, da quando si riprova da sola.
    pub installa_dal: i64,
    /// La versione gia' annunciata con una notifica.
    pub avvisata: Option<String>,
    /// La prova di aggiornamento delle macchine di GitHub e' gia' stata fatta (non si ripete al riavvio).
    pub prova_fatta: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Azione {
    Niente,
    /// Una notifica: c'e' una versione nuova da installare con "Aggiorna".
    Avvisa(String),
    Installa,
}

/// Com'e' andata l'installazione partita prima del riavvio.
#[derive(Debug, PartialEq, Eq)]
pub enum Avvio {
    Riuscito { da: String, a: String, automatica: bool },
    NonRiuscito { a: String, volte: u32 },
}

impl Canale {
    pub fn prossimo_controllo(&self) -> i64 {
        let attesa = if self.errori == 0 { OGNI } else { (RIPROVA << (self.errori - 1).min(5)).min(OGNI) };
        self.ultimo_controllo + attesa
    }

    /// E' ora di chiedere al canale. Un ultimo controllo "nel futuro" (orologio spostato e poi corretto) non deve
    /// fermare i controlli fino a quella data.
    pub fn da_controllare(&self, ora: i64) -> bool {
        ora >= self.prossimo_controllo() || self.ultimo_controllo > ora
    }

    /// Da quando una versione disponibile si installa da sola.
    pub fn dal(d: &Disponibile) -> i64 {
        if d.urgente {
            d.pubblicata
        } else {
            d.pubblicata + RODAGGIO
        }
    }

    /// L'app ha smesso di riprovare da sola questa versione.
    pub fn rinunciata(&self, versione: &str) -> bool {
        self.fallite.as_ref().is_some_and(|(v, n)| v == versione && *n >= TENTATIVI)
    }

    pub fn decidi(&self, ora: i64, modo: Modo) -> Azione {
        let Some(d) = &self.disponibile else { return Azione::Niente };
        let da_sola = modo == Modo::Automatico && !self.spenti && !self.rinunciata(&d.versione);
        if da_sola {
            // durante il rodaggio niente notifiche: arriva da sola
            let pronta = ora >= Self::dal(d) && ora >= self.installa_dal;
            return if pronta { Azione::Installa } else { Azione::Niente };
        }
        if modo == Modo::NonDisponibile || self.avvisata.as_deref() == Some(d.versione.as_str()) {
            return Azione::Niente;
        }
        Azione::Avvisa(d.versione.clone())
    }

    /// Esito di un controllo del canale. `vista` e' la versione annunciata, con la sua data di pubblicazione.
    pub fn controllato(&mut self, ora: i64, esito: Result<Option<(String, Option<i64>, bool)>, String>) {
        self.ultimo_controllo = ora;
        match esito {
            Ok(vista) => {
                self.errori = 0;
                self.ultimo_errore = None;
                self.disponibile = vista.map(|(versione, data, urgente)| {
                    // senza data, vale la prima volta che l'app l'ha vista (il rodaggio non riparte a ogni controllo)
                    let gia = self.disponibile.as_ref().filter(|d| d.versione == versione).map(|d| d.pubblicata);
                    Disponibile { pubblicata: data.or(gia).unwrap_or(ora), versione, urgente }
                });
            }
            Err(e) => {
                self.errori += 1;
                self.ultimo_errore = Some(e);
            }
        }
    }

    /// Un'installazione non partita: si riprova tra RIPROVA_INSTALLAZIONE. `conta` se e' colpa del pacchetto o del
    /// sistema (firma, copia, installer) e non della rete: dopo TENTATIVI si smette di riprovare da sola.
    pub fn non_installata(&mut self, versione: &str, ora: i64, conta: bool) {
        self.installa_dal = ora + RIPROVA_INSTALLAZIONE;
        if conta {
            let volte = match &self.fallite {
                Some((v, n)) if v == versione => n + 1,
                _ => 1,
            };
            self.fallite = Some((versione.to_string(), volte));
        }
    }

    /// All'avvio: com'e' andata l'installazione partita prima, se ce n'era una. Una versione "disponibile" non piu'
    /// nuova (installata a mano, per esempio) si dimentica.
    pub fn dopo_avvio(&mut self, versione: &str) -> Option<Avvio> {
        if self.disponibile.as_ref().is_some_and(|d| !piu_nuova(&d.versione, versione)) {
            self.disponibile = None;
        }
        let i = self.installazione.take()?;
        if i.a == versione {
            self.fallite = None;
            self.installa_dal = 0;
            return Some(Avvio::Riuscito { da: i.da, a: i.a, automatica: i.automatica });
        }
        let volte = match &self.fallite {
            Some((v, n)) if *v == i.a => n + 1,
            _ => 1,
        };
        self.fallite = Some((i.a.clone(), volte));
        self.installa_dal = i.ts + RIPROVA_INSTALLAZIONE;
        Some(Avvio::NonRiuscito { a: i.a, volte })
    }
}

fn leggi(file: &Path) -> Canale {
    // un file rovinato non deve fermare nulla: si riparte da capo (al peggio si ricontrolla prima)
    fs::read(file).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

/// "14:05" oggi, "31/10 14:05" un altro giorno.
fn ora_di(ts: i64) -> String {
    let t = Local.timestamp_opt(ts, 0).single().unwrap_or_else(Local::now);
    if t.date_naive() == Local::now().date_naive() {
        t.format("%H:%M").to_string()
    } else {
        t.format("%d/%m %H:%M").to_string()
    }
}

const OCCUPATA: &str = "C'è un controllo, uno spostamento, una prenotazione o una disdetta in corso: l'aggiornamento \
                        si installa appena finisce.";

/// Cosa serve all'installazione dal resto dell'app. Con l'app avviata e' lo `Stato`; se l'avvio non e' riuscito
/// (`Soccorso`) solo il registro: non ci sono controlli o operazioni da aspettare ne' ricette aperte da copiare.
pub trait Ambiente: Send + Sync {
    fn scrivi(&self, riga: &str);
    /// Ferma controlli e operazioni per installare: false se c'e' qualcosa in corso.
    fn ferma(&self) -> bool;
    /// L'installazione non e' partita: controlli e operazioni ripartono.
    fn riprendi(&self);
    fn copia(&self, versione: &str) -> Result<Option<PathBuf>, String>;
}

impl Ambiente for Stato {
    fn scrivi(&self, riga: &str) {
        self.registro.scrivi(riga)
    }
    fn ferma(&self) -> bool {
        self.chiudi_per_aggiornare()
    }
    fn riprendi(&self) {
        self.riapri()
    }
    fn copia(&self, versione: &str) -> Result<Option<PathBuf>, String> {
        self.archivio.copia(versione)
    }
}

/// Fa ripartire controlli e operazioni quando esce di scena, a meno che l'app non si stia riavviando.
struct Riprendi<'a>(&'a dyn Ambiente, bool);

impl Drop for Riprendi<'_> {
    fn drop(&mut self) {
        if self.1 {
            self.0.riprendi();
        }
    }
}

pub struct Soccorso {
    registro: Registro,
    /// Si sta installando: chiudere il messaggio d'errore non deve far uscire l'app a meta'.
    pub installando: Arc<AtomicBool>,
}

impl Ambiente for Soccorso {
    fn scrivi(&self, riga: &str) {
        self.registro.scrivi(riga)
    }
    fn ferma(&self) -> bool {
        self.installando.store(true, Ordering::SeqCst);
        true
    }
    fn riprendi(&self) {
        self.installando.store(false, Ordering::SeqCst);
    }
    fn copia(&self, _: &str) -> Result<Option<PathBuf>, String> {
        Ok(None) // l'archivio non si e' aperto: resta com'e', non lo tocca nessuno
    }
}

pub struct Aggiornamenti {
    file: PathBuf,
    pub versione: String,
    /// "nsis", "msi", "app"...
    pub installata_con: &'static str,
    pub modo: Modo,
    canale: Mutex<Canale>,
    /// L'ultima versione trovata, pronta da scaricare.
    trovato: Mutex<Option<Update>>,
    /// L'ultimo pacchetto scaricato e verificato: se l'installazione si rimanda, non si riscarica.
    scaricato: Mutex<Option<(String, Arc<Vec<u8>>)>>,
    /// Un controllo o un'installazione alla volta.
    lavoro: Mutex<()>,
}

/// Cosa mostra l'interfaccia.
#[derive(Serialize)]
pub struct Info {
    pub versione: String,
    pub modo: Modo,
    pub automatici: bool,
    pub testo: String,
    /// "Aggiorna alla 0.3.1", se c'e' una versione da installare.
    pub pulsante: Option<String>,
}

impl Aggiornamenti {
    pub fn new(cartella: &Path, versione: String, installata_con: Option<BundleType>) -> Self {
        let file = cartella.join(FILE);
        let canale = leggi(&file);
        let si_scrive = cartella_dell_app(installata_con.as_ref()).is_some_and(|c| scrivibile(&c)) && bundle_nostro();
        Aggiornamenti {
            file,
            versione,
            installata_con: nome_installazione(installata_con.as_ref()),
            modo: modo(installata_con.as_ref(), si_scrive),
            canale: Mutex::new(canale),
            trovato: Mutex::new(None),
            scaricato: Mutex::new(None),
            lavoro: Mutex::new(()),
        }
    }

    /// All'avvio: com'e' andata l'installazione partita prima del riavvio, se ce n'era una.
    pub fn dopo_avvio(&self) -> Result<Option<Avvio>, String> {
        let v = self.versione.clone();
        self.cambia(|c| c.dopo_avvio(&v))
    }

    pub fn canale(&self) -> Canale {
        blocca(&self.canale).clone()
    }

    /// Cambia lo stato e lo salva. Se il salvataggio non riesce l'errore torna indietro: per un'installazione vuol
    /// dire non partire (al riavvio non si saprebbe com'e' andata).
    fn cambia<T>(&self, f: impl FnOnce(&mut Canale) -> T) -> Result<T, String> {
        let mut c = blocca(&self.canale);
        let r = f(&mut c);
        let b = serde_json::to_vec_pretty(&*c).map_err(|e| e.to_string())?;
        crate::archivio::scrivi_privato(&self.file, &b)?;
        Ok(r)
    }

    pub fn imposta(&self, automatici: bool) -> Result<(), String> {
        self.cambia(|c| {
            c.spenti = !automatici;
            c.avvisata = None; // spenti: la versione disponibile si annuncia di nuovo
        })
    }

    pub fn info(&self) -> Info {
        let c = self.canale();
        let automatici = !c.spenti;
        let (testo, pulsante) = match (&c.disponibile, self.modo) {
            (_, Modo::NonDisponibile) => (
                "Questa copia dell'app non si aggiorna da sola: le versioni nuove si scaricano dalla pagina dell'app."
                    .to_string(),
                None,
            ),
            (Some(d), _) if c.rinunciata(&d.versione) => (
                format!(
                    "L'installazione della versione {} non è riuscita {TENTATIVI} volte: riprova con il pulsante o \
                     scaricala dalla pagina dell'app.",
                    d.versione
                ),
                Some(format!("Aggiorna alla {}", d.versione)),
            ),
            (Some(d), Modo::Automatico) if automatici => (
                if adesso() < Canale::dal(d) {
                    format!(
                        "C'è la versione {}: si installa da sola dal {}, quando non ci sono controlli o operazioni in \
                         corso. Per averla subito: Aggiorna.",
                        d.versione,
                        ora_di(Canale::dal(d))
                    )
                } else {
                    format!(
                        "C'è la versione {}: si installa appena non ci sono controlli o operazioni in corso.",
                        d.versione
                    )
                },
                Some(format!("Aggiorna alla {}", d.versione)),
            ),
            (Some(d), Modo::Automatico) => (
                format!("C'è la versione {}. Gli aggiornamenti automatici sono spenti.", d.versione),
                Some(format!("Aggiorna alla {}", d.versione)),
            ),
            (Some(d), _) => (
                format!(
                    "C'è la versione {}. Con questa installazione il sistema chiede la password di amministratore: si \
                     installa solo quando tocchi Aggiorna.",
                    d.versione
                ),
                Some(format!("Aggiorna alla {}", d.versione)),
            ),
            (None, _) if c.errori > 0 => (
                format!(
                    "Non riesco a controllare se ci sono versioni nuove (da {} volte di fila): riprovo alle {}.",
                    c.errori,
                    ora_di(c.prossimo_controllo())
                ),
                None,
            ),
            (None, _) if c.ultimo_controllo > 0 => {
                (format!("È l'ultima versione (controllato alle {}).", ora_di(c.ultimo_controllo)), None)
            }
            (None, _) => ("Il primo controllo degli aggiornamenti è tra qualche minuto.".to_string(), None),
        };
        Info { versione: self.versione.clone(), modo: self.modo, automatici, testo, pulsante }
    }

    /// Chiede al canale se c'e' una versione nuova. Si chiama da un thread qualsiasi, non dal runtime async.
    pub fn controlla(&self, app: &AppHandle, amb: &dyn Ambiente) -> Result<Option<String>, String> {
        let _uno = blocca(&self.lavoro);
        let esito =
            tauri::async_runtime::block_on(async { app.updater_builder().timeout(ATTESA_RETE).build()?.check().await });
        let (vista, errore) = match esito {
            Ok(Some(u)) => {
                let data = u.date.map(|d| d.unix_timestamp());
                let urgente = u.raw_json.get("urgente").and_then(|v| v.as_bool()).unwrap_or(false);
                let v = u.version.clone();
                *blocca(&self.trovato) = Some(u);
                (Ok(Some((v, data, urgente))), None)
            }
            Ok(None) => {
                *blocca(&self.trovato) = None;
                (Ok(None), None)
            }
            Err(e) => {
                let e = senza_percorsi(&e.to_string());
                (Err(e.clone()), Some(e))
            }
        };
        let versione = vista.as_ref().ok().and_then(|v| v.as_ref().map(|(v, _, _)| v.clone()));
        let c = self.cambia(|c| {
            c.controllato(adesso(), vista);
            c.errori
        });
        match (&errore, &versione) {
            (Some(e), _) => amb.scrivi(&format!(
                "aggiornamenti: controllo non riuscito ({} di fila): {e}",
                c.as_ref().copied().unwrap_or(0)
            )),
            (None, Some(v)) => amb.scrivi(&format!("aggiornamenti: disponibile la versione {v}")),
            (None, None) => amb.scrivi("aggiornamenti: nessuna versione nuova"),
        }
        if let Err(e) = c {
            amb.scrivi(&format!("aggiornamenti: stato non salvato: {}", senza_percorsi(&e)));
        }
        match errore {
            Some(e) => Err(format!("Non riesco a controllare gli aggiornamenti: {e}")),
            None => Ok(versione),
        }
    }

    /// Scarica (la firma si verifica qui) e installa la versione trovata. Su Windows l'app esce e l'installer la
    /// riavvia; altrove si riavvia da sola. Torna indietro solo se non si e' installato nulla.
    pub fn installa(&self, app: &AppHandle, amb: &dyn Ambiente, automatica: bool) -> Result<(), String> {
        let _uno = blocca(&self.lavoro);
        let mut u = blocca(&self.trovato).clone().ok_or("Nessun aggiornamento da installare: controlla di nuovo.")?;
        u.timeout = Some(ATTESA_DOWNLOAD);
        let v = u.version.clone();
        let gia = blocca(&self.scaricato).clone().filter(|(sv, _)| *sv == v).map(|(_, b)| b);
        let pacchetto = match gia {
            Some(b) => b,
            None => {
                amb.scrivi(&format!("aggiornamenti: scarico la versione {v}"));
                match tauri::async_runtime::block_on(u.download(|_, _| {}, || {})) {
                    Ok(b) => {
                        let b = Arc::new(b);
                        *blocca(&self.scaricato) = Some((v.clone(), b.clone()));
                        b
                    }
                    Err(e) => {
                        // la rete si riprova senza contare; una firma sbagliata (il download la verifica) conta
                        let rete = matches!(
                            e,
                            tauri_plugin_updater::Error::Reqwest(_)
                                | tauri_plugin_updater::Error::Network(_)
                                | tauri_plugin_updater::Error::Io(_)
                        );
                        let e = senza_percorsi(&e.to_string());
                        amb.scrivi(&format!("aggiornamenti: versione {v} non scaricata: {e}"));
                        let _ = self.cambia(|c| c.non_installata(&v, adesso(), !rete));
                        return Err(format!("Non riesco a scaricare la versione {v}: {e}"));
                    }
                }
            }
        };
        self.installa_pacchetto(app, amb, &u, &pacchetto, &v, automatica)
    }

    /// L'installazione vera e propria. Parte solo se non c'e' niente in corso e da li' non parte piu' nulla (su
    /// Windows l'app esce, altrove si riavvia); se qualcosa non va, controlli e operazioni riprendono.
    fn installa_pacchetto(
        &self,
        app: &AppHandle,
        amb: &dyn Ambiente,
        u: &Update,
        pacchetto: &[u8],
        a: &str,
        automatica: bool,
    ) -> Result<(), String> {
        if !amb.ferma() {
            return Err(OCCUPATA.into());
        }
        let mut guardia = Riprendi(amb, true);
        // (messaggio, se conta come installazione non riuscita)
        let esito = (|| -> Result<(), (String, bool)> {
            // la firma di nuovo, sui byte che si installano davvero
            u.verify(pacchetto).map_err(|e| {
                *blocca(&self.scaricato) = None;
                amb.scrivi(&format!("aggiornamenti: firma della versione {} non valida: {e}", u.version));
                (format!("La versione {} non ha una firma valida: non la installo.", u.version), true)
            })?;
            let copia = amb.copia(&self.versione).map_err(|e| {
                let e = senza_percorsi(&e);
                amb.scrivi(&format!("aggiornamenti: copia delle ricette non riuscita, non installo: {e}"));
                (format!("{e}: per sicurezza non installo l'aggiornamento."), true)
            })?;
            let i = Installazione { da: self.versione.clone(), a: a.to_string(), ts: adesso(), automatica };
            self.cambia(|c| c.installazione = Some(i)).map_err(|e| (senza_percorsi(&e), false))?;
            amb.scrivi(&format!(
                "aggiornamenti: installo la versione {} ({}){}",
                u.version,
                if automatica { "da sola" } else { "chiesto dall'utente" },
                if copia.is_some() { ", copia delle ricette fatta" } else { "" }
            ));
            u.install(pacchetto).map_err(|e| {
                let _ = self.cambia(|c| c.installazione = None);
                let e = senza_percorsi(&e.to_string());
                amb.scrivi(&format!("aggiornamenti: installazione non riuscita: {e}"));
                (format!("Installazione non riuscita: {e}"), true)
            })
        })();
        match esito {
            // Windows non arriva qui: l'installer ha gia' fatto uscire l'app
            Ok(()) => {
                guardia.1 = false;
                app.restart()
            }
            Err((messaggio, conta)) => {
                let _ = self.cambia(|c| c.non_installata(a, adesso(), conta));
                Err(messaggio) // la guardia fa ripartire controlli e operazioni
            }
        }
    }

    /// Prova di aggiornamento delle macchine di GitHub (CI=true): installa sopra l'app il pacchetto indicato da
    /// CUP_PROVA_AGGIORNAMENTO, con la firma accanto (.sig), passando dallo stesso codice di un aggiornamento vero.
    /// Prova la chiave dentro l'app, il riconoscimento del tipo di installazione, la copia, l'installer e il riavvio.
    /// Una volta sola: al riavvio il registro deve dire "aggiornamenti: aggiornata".
    pub fn prova_ci(&self, app: &AppHandle, amb: &dyn Ambiente) {
        let Some(pacchetto) = std::env::var_os("CUP_PROVA_AGGIORNAMENTO") else { return };
        if std::env::var("CI").as_deref() != Ok("true") || self.canale().prova_fatta {
            return;
        }
        let esito = (|| -> Result<(), String> {
            let pacchetto = PathBuf::from(pacchetto);
            let mut sig = pacchetto.clone().into_os_string();
            sig.push(".sig");
            let firma = fs::read_to_string(&sig).map_err(|e| format!("firma: {e}"))?;
            let b = fs::read(&pacchetto).map_err(|e| format!("pacchetto: {e}"))?;
            // ogni nome di piattaforma che pubblica il canale: deve trovare il suo
            let mut piattaforme = serde_json::Map::new();
            for t in TARGET {
                piattaforme.insert(
                    t.to_string(),
                    serde_json::json!({"url": "https://example.invalid/prova", "signature": firma.trim()}),
                );
            }
            let manifesto = serde_json::json!({
                "version": self.versione,
                "pub_date": chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
                "platforms": piattaforme,
            });
            let u = app
                .updater_builder()
                .version_comparator(|_, _| true) // stessa versione: e' una prova
                .build()
                .and_then(|x| x.restore_update(manifesto))
                .map_err(|e| e.to_string())?
                .ok_or("nessun aggiornamento dal manifesto di prova")?;
            amb.scrivi(&format!("aggiornamenti: prova, piattaforma {}", u.target));
            self.cambia(|c| c.prova_fatta = true)?;
            let v = self.versione.clone();
            self.installa_pacchetto(app, amb, &u, &b, &v, true)
        })();
        if let Err(e) = esito {
            amb.scrivi(&format!("aggiornamenti: prova non riuscita: {}", senza_percorsi(&e)));
        }
    }
}

/// I nomi delle piattaforme nel canale (vedi .github/scripts/canale-aggiornamenti.py).
pub const TARGET: [&str; 9] = [
    "windows-x86_64-nsis",
    "windows-x86_64-msi",
    "windows-x86_64",
    "darwin-aarch64",
    "darwin-x86_64",
    "linux-x86_64-appimage",
    "linux-x86_64-deb",
    "linux-x86_64-rpm",
    "linux-x86_64",
];

/// Un giro del thread: controlla quando e' ora, installa quando si puo', avvisa quando serve l'utente.
fn passo(app: &AppHandle, amb: &dyn Ambiente, agg: &Aggiornamenti, avvisa: &dyn Fn(&str, &str, &str)) {
    if agg.canale().da_controllare(adesso()) {
        let _ = agg.controlla(app, amb);
    }
    match agg.canale().decidi(adesso(), agg.modo) {
        Azione::Installa => {
            // subito prima di installare si rilegge il canale: una versione ritirata nel frattempo non si installa
            let vecchio = agg.canale().ultimo_controllo + RILEGGI < adesso() || blocca(&agg.trovato).is_none();
            if vecchio && agg.controlla(app, amb).is_err() {
                return;
            }
            if agg.canale().decidi(adesso(), agg.modo) != Azione::Installa {
                return;
            }
            // OCCUPATA e' normale (un controllo in corso): si riprova al prossimo passo, senza scriverlo
            if let Err(e) = agg.installa(app, amb, true) {
                if e != OCCUPATA {
                    amb.scrivi(&format!("aggiornamenti: rimandato: {e}"));
                }
            }
        }
        Azione::Avvisa(v) => {
            avvisa(
                &format!("CUP Piemonte: c'è la versione {v}"),
                "Apri l'app, poi ⚙ e Aggiorna.",
                &format!("aggiornamento disponibile, versione {v}"),
            );
            let _ = agg.cambia(|c| c.avvisata = Some(v));
        }
        Azione::Niente => {}
    }
}

/// Il thread degli aggiornamenti.
pub fn avvia(
    app: AppHandle,
    amb: Arc<dyn Ambiente>,
    agg: Arc<Aggiornamenti>,
    avvisa: impl Fn(&str, &str, &str) + Send + 'static,
) {
    if agg.modo == Modo::NonDisponibile {
        amb.scrivi("aggiornamenti: non disponibili per questa installazione");
        return;
    }
    thread::spawn(move || {
        thread::sleep(PRIMO);
        loop {
            let giro = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| passo(&app, &*amb, &agg, &avvisa)));
            if giro.is_err() {
                amb.riprendi(); // un panico a meta' non deve lasciare l'app ferma
                amb.scrivi("aggiornamenti: errore interno, riprovo piu' tardi");
            }
            thread::sleep(PASSO);
        }
    });
}

/// L'avvio non e' riuscito (per esempio le ricette non si aprono): se il canale ha una versione nuova la si installa.
/// Una versione difettosa cosi' si corregge da lontano. Senza rodaggio se questa versione si e' appena installata
/// (si e' rotta subito) o se la correzione e' "urgente"; altrimenti (un errore passeggero, come il portachiavi che
/// non risponde) con le regole di sempre. Ritorna il segnale "installazione in corso": il messaggio d'errore, se
/// chiuso, non deve far uscire l'app a meta'.
pub fn soccorso(
    app: AppHandle,
    registro: Registro,
    cartella: &Path,
    versione: String,
    installata_con: Option<BundleType>,
) -> Arc<AtomicBool> {
    let agg = Aggiornamenti::new(cartella, versione, installata_con);
    let installando = Arc::new(AtomicBool::new(false));
    let amb = Soccorso { registro, installando: installando.clone() };
    let appena = match agg.dopo_avvio() {
        Ok(Some(Avvio::Riuscito { .. })) => true,
        Ok(Some(a)) => {
            amb.scrivi(&format!("aggiornamenti: dopo l'installazione: {a:?}"));
            false
        }
        _ => false,
    };
    if agg.modo != Modo::Automatico || agg.canale().spenti {
        return installando;
    }
    thread::spawn(move || {
        if let Ok(Some(v)) = agg.controlla(&app, &amb) {
            if appena || agg.canale().decidi(adesso(), agg.modo) == Azione::Installa {
                amb.scrivi(&format!("aggiornamenti: avvio non riuscito, installo la versione {v}"));
                let _ = agg.installa(&app, &amb, true);
            }
        }
    });
    installando
}

#[cfg(test)]
mod test {
    use super::*;

    const ORA: i64 = 1_800_000_000;

    fn disponibile(versione: &str, pubblicata: i64) -> Canale {
        Canale {
            disponibile: Some(Disponibile { versione: versione.into(), pubblicata, urgente: false }),
            ..Default::default()
        }
    }

    #[test]
    fn da_sola_solo_dove_non_serve_una_password() {
        assert_eq!(modo(Some(&BundleType::Nsis), true), Modo::Automatico);
        assert_eq!(modo(Some(&BundleType::AppImage), true), Modo::Automatico);
        assert_eq!(modo(Some(&BundleType::App), true), Modo::Automatico);
        // un'app di macOS installata da un amministratore: per sostituirla serve la password
        assert_eq!(modo(Some(&BundleType::App), false), Modo::ARichiesta);
        assert_eq!(modo(Some(&BundleType::Msi), true), Modo::ARichiesta);
        assert_eq!(modo(Some(&BundleType::Deb), true), Modo::ARichiesta);
        assert_eq!(modo(Some(&BundleType::Rpm), true), Modo::ARichiesta);
        assert_eq!(modo(None, true), Modo::NonDisponibile);
    }

    #[test]
    fn controlli_ogni_sei_ore_e_dopo_un_errore_prima() {
        let mut c = Canale::default();
        c.controllato(ORA, Ok(None));
        assert_eq!(c.prossimo_controllo(), ORA + OGNI);
        assert!(!c.da_controllare(ORA + OGNI - 1) && c.da_controllare(ORA + OGNI));
        // orologio rimesso indietro: l'ultimo controllo e' "nel futuro", si controlla subito
        assert!(c.da_controllare(ORA - 3600));
        c.controllato(ORA, Err("rete".into()));
        assert_eq!(c.prossimo_controllo(), ORA + RIPROVA);
        c.controllato(ORA, Err("rete".into()));
        assert_eq!(c.prossimo_controllo(), ORA + 2 * RIPROVA);
        for _ in 0..20 {
            c.controllato(ORA, Err("rete".into()));
        }
        assert_eq!(c.prossimo_controllo(), ORA + OGNI); // mai piu' radi del normale
        assert_eq!(c.ultimo_errore.as_deref(), Some("rete"));
        c.controllato(ORA, Ok(None));
        assert_eq!((c.errori, c.ultimo_errore.clone()), (0, None));
    }

    #[test]
    fn rodaggio_di_due_giorni_tranne_se_urgente() {
        let c = disponibile("0.3.1", ORA);
        assert_eq!(c.decidi(ORA + RODAGGIO - 1, Modo::Automatico), Azione::Niente);
        assert_eq!(c.decidi(ORA + RODAGGIO, Modo::Automatico), Azione::Installa);
        let mut u = c.clone();
        u.disponibile.as_mut().unwrap().urgente = true;
        assert_eq!(u.decidi(ORA, Modo::Automatico), Azione::Installa);
        // la data e' quella del canale; senza, la prima volta che si e' vista (il rodaggio non riparte)
        let mut c = Canale::default();
        c.controllato(ORA, Ok(Some(("0.3.1".into(), None, false))));
        c.controllato(ORA + 3600, Ok(Some(("0.3.1".into(), None, false))));
        assert_eq!(c.disponibile.as_ref().unwrap().pubblicata, ORA);
        c.controllato(ORA + 7200, Ok(Some(("0.3.1".into(), Some(ORA - 10), false))));
        assert_eq!(c.disponibile.as_ref().unwrap().pubblicata, ORA - 10);
        // ritirata dal canale: non si installa piu'
        c.controllato(ORA + 9000, Ok(None));
        assert_eq!(c.decidi(ORA + 10 * RODAGGIO, Modo::Automatico), Azione::Niente);
    }

    #[test]
    fn con_la_password_o_spenti_solo_una_notifica() {
        let mut c = disponibile("0.3.1", ORA);
        assert_eq!(c.decidi(ORA + RODAGGIO, Modo::ARichiesta), Azione::Avvisa("0.3.1".into()));
        c.avvisata = Some("0.3.1".into());
        assert_eq!(c.decidi(ORA + RODAGGIO, Modo::ARichiesta), Azione::Niente);
        let mut c = disponibile("0.3.1", ORA);
        c.spenti = true;
        assert_eq!(c.decidi(ORA, Modo::Automatico), Azione::Avvisa("0.3.1".into()));
        assert_eq!(c.decidi(ORA + RODAGGIO, Modo::NonDisponibile), Azione::Niente);
    }

    #[test]
    fn non_installata_si_riprova_tra_un_ora_e_poi_si_smette() {
        let t = ORA + RODAGGIO;
        let mut c = disponibile("0.3.1", ORA);
        // la rete: si riprova tra un'ora, senza contarla
        c.non_installata("0.3.1", t, false);
        assert_eq!(c.decidi(t + 60, Modo::Automatico), Azione::Niente);
        assert_eq!(c.decidi(t + RIPROVA_INSTALLAZIONE, Modo::Automatico), Azione::Installa);
        assert_eq!(c.fallite, None);
        // firma, copia o installer: conta
        c.non_installata("0.3.1", t, true);
        c.non_installata("0.3.1", t, true);
        assert!(c.rinunciata("0.3.1"));
        assert_eq!(c.decidi(t + 10 * RIPROVA_INSTALLAZIONE, Modo::Automatico), Azione::Avvisa("0.3.1".into()));
    }

    #[test]
    fn al_riavvio_si_controlla_la_versione() {
        let installa = |c: &mut Canale| {
            c.installazione = Some(Installazione { da: "0.3.0".into(), a: "0.3.1".into(), ts: ORA, automatica: true })
        };
        let mut c = disponibile("0.3.1", ORA - RODAGGIO);
        assert_eq!(c.dopo_avvio("0.3.0"), None); // nessuna installazione partita
        installa(&mut c);
        assert_eq!(c.dopo_avvio("0.3.0"), Some(Avvio::NonRiuscito { a: "0.3.1".into(), volte: 1 }));
        // si riprova, ma tra un'ora
        assert_eq!(c.decidi(ORA + 60, Modo::Automatico), Azione::Niente);
        assert_eq!(c.decidi(ORA + RIPROVA_INSTALLAZIONE, Modo::Automatico), Azione::Installa);
        installa(&mut c);
        assert_eq!(c.dopo_avvio("0.3.0"), Some(Avvio::NonRiuscito { a: "0.3.1".into(), volte: 2 }));
        // TENTATIVI volte: non si riprova da sola, lo si dice una volta
        assert!(c.rinunciata("0.3.1"));
        assert_eq!(c.decidi(ORA + RODAGGIO, Modo::Automatico), Azione::Avvisa("0.3.1".into()));
        // una versione dopo si riprova
        c.disponibile = Some(Disponibile { versione: "0.3.2".into(), pubblicata: ORA - RODAGGIO, urgente: false });
        assert_eq!(c.decidi(ORA + RIPROVA_INSTALLAZIONE, Modo::Automatico), Azione::Installa);
        // riuscita con "Aggiorna"
        c.disponibile = Some(Disponibile { versione: "0.3.1".into(), pubblicata: ORA, urgente: false });
        installa(&mut c);
        assert_eq!(
            c.dopo_avvio("0.3.1"),
            Some(Avvio::Riuscito { da: "0.3.0".into(), a: "0.3.1".into(), automatica: true })
        );
        assert_eq!((c.fallite.clone(), c.disponibile.clone(), c.installazione.clone()), (None, None, None));
        // installata a mano: la "disponibile" non e' piu' nuova e si dimentica
        let mut c = disponibile("0.3.1", ORA);
        assert_eq!(c.dopo_avvio("0.3.1"), None);
        assert_eq!(c.disponibile, None);
        let mut c = disponibile("0.3.10", ORA);
        c.dopo_avvio("0.3.9");
        assert!(c.disponibile.is_some()); // confronto fra versioni, non fra testi
    }

    #[test]
    fn nel_registro_niente_cartella_dell_utente() {
        let casa = std::env::var("USERPROFILE").or_else(|_| std::env::var("HOME")).unwrap();
        let t = senza_percorsi(&format!("file {casa}{}x.tmp non trovato", std::path::MAIN_SEPARATOR));
        assert!(!t.contains(&casa) && t.contains("~"), "{t}");
    }

    #[test]
    fn stato_rovinato_o_di_una_versione_vecchia() {
        let c = std::env::temp_dir().join(format!("cup-test-agg-{}", std::process::id()));
        let _ = fs::remove_dir_all(&c);
        fs::create_dir_all(&c).unwrap();
        fs::write(c.join(FILE), b"{rotto").unwrap();
        assert_eq!(leggi(&c.join(FILE)), Canale::default());
        fs::write(c.join(FILE), br#"{"spenti":true,"campo_nuovo":1}"#).unwrap();
        assert!(leggi(&c.join(FILE)).spenti);
        let a = Aggiornamenti::new(&c, "0.3.0".into(), Some(BundleType::Nsis));
        a.imposta(true).unwrap();
        assert!(!leggi(&c.join(FILE)).spenti);
        assert!(scrivibile(&c));
    }
}
