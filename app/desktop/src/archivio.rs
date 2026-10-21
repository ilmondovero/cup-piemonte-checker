//! Le ricette salvate sul computer: un file cifrato (XChaCha20-Poly1305) con la chiave nel portachiavi del
//! sistema (Gestione credenziali di Windows, Portachiavi di macOS, Secret Service su Linux). Se il portachiavi
//! non c'e' (un Linux senza Secret Service) e non ci sono ricette da perdere, la chiave sta in un file accanto
//! ai dati: meno sicuro, e l'interfaccia lo dice.

use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use chacha20poly1305::aead::{Aead, AeadCore, KeyInit, OsRng};
use chacha20poly1305::{Key, XChaCha20Poly1305, XNonce};
use serde::{Deserialize, Serialize};

const FILE: &str = "ricette.cifrate";
const FILE_CHIAVE: &str = "chiave.segreta";
const INTESTAZIONE: &[u8] = b"CUP1";
const SERVIZIO: &str = "cup-piemonte";
const UTENTE: &str = "chiave-archivio";
/// Date gia' notificate ricordate per ricetta: le piu' vecchie si dimenticano.
pub const NOTIFICATE_MAX: usize = 300;

/// La prenotazione come la mostra l'app.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Visita {
    pub quando: String,
    pub luogo: String,
    pub cosa: String,
}

/// Una data buona trovata da un controllo.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Data {
    /// Data e luogo: la stessa data trovata in due controlli ha la stessa chiave (per non notificarla due volte).
    pub chiave: String,
    pub quando: String,
    pub luogo: String,
    pub proposta: bool,
}

/// Com'e' andato l'ultimo controllo.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Esito {
    /// Secondi dal 1970.
    pub ts: i64,
    /// Viene da un controllo delle date (non solo dalla lettura della prenotazione, come quando si aggiunge).
    #[serde(default)]
    pub controllo: bool,
    pub attuale: Option<Visita>,
    /// La zona descritta con la prenotazione letta ("solo nel comune di Torino").
    #[serde(default)]
    pub zona: String,
    /// Quante date ha offerto il portale.
    pub date: usize,
    /// Quelle che vanno bene, dalla piu' vicina.
    pub buone: Vec<Data>,
    pub area_incompleta: bool,
    pub errore: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ricetta {
    pub id: u64,
    pub nome: String,
    pub cf: String,
    pub nre: String,
    /// La zona come la sceglie l'utente (vedi cup_core::Dove::da_scelta).
    pub dove: String,
    pub valore: String,
    /// Giorni della settimana da evitare, lunedi' = 0.
    pub evita: Vec<u32>,
    /// I controlli automatici sono accesi.
    pub attiva: bool,
    /// L'ultima volta che un controllo e' partito (secondi dal 1970): da li' si contano i 20 minuti.
    #[serde(default)]
    pub ultimo_tentativo: Option<i64>,
    #[serde(default)]
    pub esito: Option<Esito>,
    /// Le chiavi delle date gia' notificate.
    #[serde(default)]
    pub notificate: Vec<String>,
    /// Controlli di fila in cui il portale non ha trovato la prenotazione (a volte sbaglia: si sospende dopo 3).
    #[serde(default)]
    pub assenze: u32,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Dati {
    pub prossimo_id: u64,
    pub ricette: Vec<Ricetta>,
    /// (numero ricetta, ultimo controllo) delle ricette eliminate da poco: riaggiungerle non deve far ripartire
    /// subito "Sposta" (la data bloccata dal controllo di prima e' ancora bloccata). Si tengono per 20 minuti.
    #[serde(default)]
    pub eliminate: Vec<(String, i64)>,
}

impl Dati {
    pub fn ricetta(&mut self, id: u64) -> Result<&mut Ricetta, String> {
        self.ricette.iter_mut().find(|r| r.id == id).ok_or_else(|| "Ricetta non trovata".to_string())
    }

    /// Elimina la ricetta, ricordando per `intervallo` secondi quando e' stata controllata l'ultima volta.
    pub fn elimina(&mut self, id: u64, ora: i64, intervallo: i64) {
        let recenti: Vec<(String, i64)> = self
            .ricette
            .iter()
            .filter(|r| r.id == id)
            .filter_map(|r| r.ultimo_tentativo.filter(|t| ora - t < intervallo).map(|t| (r.nre.clone(), t)))
            .collect();
        self.eliminate.retain(|(_, t)| ora - t < intervallo);
        self.eliminate.extend(recenti);
        self.ricette.retain(|r| r.id != id);
    }

    /// L'ultimo controllo di una ricetta con questo numero eliminata da meno di `intervallo` secondi.
    pub fn ultimo_eliminata(&mut self, nre: &str, ora: i64, intervallo: i64) -> Option<i64> {
        self.eliminate.retain(|(_, t)| ora - t < intervallo);
        self.eliminate.iter().filter(|(n, _)| n == nre).map(|(_, t)| *t).max()
    }
}

/// Dove sta la chiave dell'archivio.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Protezione {
    Portachiavi,
    File,
}

/// Perche' l'archivio non si legge.
#[derive(Debug)]
enum Lettura {
    /// Il disco non risponde: si riprova, senza toccare nulla.
    Disco(String),
    /// Intestazione o lunghezza sbagliate: il file e' rovinato (per esempio troncato).
    Rovinato,
    /// La chiave non e' quella con cui e' stato cifrato.
    Chiave,
    /// Decifrato ma non si capisce (una versione dell'app piu' nuova?): non si tocca.
    Formato(String),
}

impl fmt::Display for Lettura {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Lettura::Disco(e) => write!(f, "Non riesco a leggere le ricette: {e}"),
            Lettura::Rovinato => write!(f, "Archivio delle ricette rovinato"),
            Lettura::Chiave => write!(f, "Non riesco a decifrare le ricette (chiave diversa?)"),
            Lettura::Formato(e) => write!(f, "Archivio delle ricette di un'altra versione dell'app: {e}"),
        }
    }
}

pub struct Archivio {
    file: PathBuf,
    chiave: Key,
    pub protezione: Protezione,
    /// Un archivio che non si poteva aprire (chiave diversa, file rovinato) e' stato messo da parte con questo nome.
    pub messo_da_parte: Option<PathBuf>,
}

fn esadecimale(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn da_esadecimale(s: &str) -> Option<Key> {
    let b: Vec<u8> = (0..s.len())
        .step_by(2)
        .map(|i| s.get(i..i + 2).and_then(|x| u8::from_str_radix(x, 16).ok()))
        .collect::<Option<_>>()?;
    (b.len() == 32).then(|| *Key::from_slice(&b))
}

/// Scrive un file solo per l'utente: prima uno temporaneo, scritto davvero sul disco, poi lo sostituisce (mai un
/// file a meta', nemmeno se il computer si spegne di colpo).
fn scrivi_privato(percorso: &Path, contenuto: &[u8]) -> Result<(), String> {
    let errore = |e: std::io::Error| format!("Non riesco a salvare: {e}");
    let tmp = percorso.with_extension("tmp");
    let _ = fs::remove_file(&tmp); // i permessi valgono solo per un file nuovo
    let mut o = OpenOptions::new();
    o.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        o.mode(0o600);
    }
    let mut f = o.open(&tmp).map_err(errore)?;
    f.write_all(contenuto).map_err(errore)?;
    f.sync_all().map_err(errore)?;
    drop(f);
    fs::rename(&tmp, percorso).map_err(errore)?;
    #[cfg(unix)]
    if let Some(cartella) = percorso.parent() {
        let _ = fs::File::open(cartella).and_then(|c| c.sync_all());
    }
    Ok(())
}

impl Archivio {
    /// Apre l'archivio nella cartella dei dati dell'app, con la chiave del portachiavi (creandola la prima volta).
    pub fn apri(cartella: &Path) -> Result<Self, String> {
        Self::apri_con(cartella, chiave_dal_portachiavi)
    }

    fn apri_con(cartella: &Path, portachiavi: impl FnOnce() -> Result<Key, String>) -> Result<Self, String> {
        fs::create_dir_all(cartella).map_err(|e| format!("Non riesco a creare la cartella dei dati: {e}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(cartella, fs::Permissions::from_mode(0o700));
        }
        let file = cartella.join(FILE);
        let file_chiave = cartella.join(FILE_CHIAVE);
        // una chiave in un file vale per prima: e' quella con cui sono stati cifrati i dati
        let (chiave, protezione) = if file_chiave.exists() {
            (chiave_dal_file(&file_chiave)?, Protezione::File)
        } else {
            match portachiavi() {
                Ok(chiave) => (chiave, Protezione::Portachiavi),
                // con ricette salvate una chiave nuova le renderebbe illeggibili: meglio fermarsi e riprovare
                Err(e) if file.exists() => {
                    return Err(format!(
                        "Il portachiavi del sistema non risponde ({e}): adesso le ricette salvate non si possono \
                         aprire. Riprova tra poco."
                    ))
                }
                Err(_) => (chiave_dal_file(&file_chiave)?, Protezione::File),
            }
        };
        let mut a = Archivio { file, chiave, protezione, messo_da_parte: None };
        match a.leggi_dati() {
            Ok(_) => {}
            // chiave diversa (portachiavi cancellato, profilo copiato) o file rovinato: non si butta nulla
            Err(Lettura::Chiave | Lettura::Rovinato) => {
                let nome = format!("{FILE}.illeggibile-{}", chrono::Local::now().format("%Y%m%d-%H%M%S"));
                let parte = cartella.join(nome);
                fs::rename(&a.file, &parte).map_err(|e| format!("Non riesco a mettere da parte l'archivio: {e}"))?;
                a.messo_da_parte = Some(parte);
            }
            Err(e) => return Err(e.to_string()),
        }
        Ok(a)
    }

    #[cfg(test)]
    pub fn con_chiave(file: PathBuf, chiave: Key) -> Self {
        Archivio { file, chiave, protezione: Protezione::File, messo_da_parte: None }
    }

    fn leggi_dati(&self) -> Result<Dati, Lettura> {
        let b = match fs::read(&self.file) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Dati::default()),
            Err(e) => return Err(Lettura::Disco(e.to_string())),
        };
        let resto = b.strip_prefix(INTESTAZIONE).filter(|r| r.len() > 24).ok_or(Lettura::Rovinato)?;
        let (nonce, cifrato) = resto.split_at(24);
        let chiaro = XChaCha20Poly1305::new(&self.chiave)
            .decrypt(XNonce::from_slice(nonce), cifrato)
            .map_err(|_| Lettura::Chiave)?;
        serde_json::from_slice(&chiaro)// il testo di serde_json riporta il valore letto (codice fiscale, ricetta...): solo la posizione
            .map_err(|e| Lettura::Formato(format!("dato non riconosciuto a riga {}, colonna {}", e.line(), e.column())))
    }

    pub fn leggi(&self) -> Result<Dati, String> {
        self.leggi_dati().map_err(|e| e.to_string())
    }

    pub fn scrivi(&self, dati: &Dati) -> Result<(), String> {
        let chiaro = serde_json::to_vec(dati).map_err(|e| e.to_string())?;
        let nonce = XChaCha20Poly1305::generate_nonce(&mut OsRng);
        let cifrato = XChaCha20Poly1305::new(&self.chiave)
            .encrypt(&nonce, chiaro.as_slice())
            .map_err(|_| "Non riesco a cifrare le ricette".to_string())?;
        scrivi_privato(&self.file, &[INTESTAZIONE, nonce.as_slice(), &cifrato].concat())
    }
}

fn chiave_dal_portachiavi() -> Result<Key, String> {
    let voce = keyring::Entry::new(SERVIZIO, UTENTE).map_err(|e| e.to_string())?;
    match voce.get_password() {
        Ok(s) => da_esadecimale(&s).ok_or_else(|| "chiave nel portachiavi non valida".to_string()),
        Err(keyring::Error::NoEntry) => {
            let chiave = XChaCha20Poly1305::generate_key(&mut OsRng);
            voce.set_password(&esadecimale(&chiave)).map_err(|e| e.to_string())?;
            // rilettura: alcuni portachiavi accettano la scrittura ma non conservano nulla
            match voce.get_password().ok().and_then(|s| da_esadecimale(&s)) {
                Some(k) if k == chiave => Ok(chiave),
                _ => Err("il portachiavi non conserva la chiave".into()),
            }
        }
        Err(e) => Err(e.to_string()),
    }
}

/// La chiave nel file; la crea solo se il file non c'e' (mai al posto di una chiave che non si riesce a leggere).
fn chiave_dal_file(file: &Path) -> Result<Key, String> {
    match fs::read_to_string(file) {
        Ok(s) => da_esadecimale(s.trim()).ok_or_else(|| "File della chiave rovinato".to_string()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            let chiave = XChaCha20Poly1305::generate_key(&mut OsRng);
            scrivi_privato(file, esadecimale(&chiave).as_bytes())?;
            Ok(chiave)
        }
        Err(e) => Err(format!("Non riesco a leggere la chiave delle ricette: {e}")),
    }
}

#[cfg(test)]
mod test {
    use super::*;

    fn cartella(nome: &str) -> PathBuf {
        let c = std::env::temp_dir().join(format!("cup-test-{nome}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&c);
        fs::create_dir_all(&c).unwrap();
        c
    }

    fn ricetta() -> Ricetta {
        Ricetta {
            id: 1,
            nome: "Prova".into(),
            cf: "RSSMRA80A01L219X".into(),
            nre: "010A00000000001".into(),
            dove: "comune".into(),
            valore: String::new(),
            evita: vec![4],
            attiva: true,
            ultimo_tentativo: None,
            esito: None,
            notificate: vec![],
            assenze: 0,
        }
    }

    fn dati() -> Dati {
        Dati { prossimo_id: 2, ricette: vec![ricetta()], eliminate: vec![] }
    }

    fn nuova_chiave() -> Key {
        XChaCha20Poly1305::generate_key(&mut OsRng)
    }

    #[test]
    fn cifra_e_rilegge() {
        let c = cartella("cifra");
        let a = Archivio::con_chiave(c.join(FILE), nuova_chiave());
        assert_eq!(a.leggi().unwrap(), Dati::default()); // nessun file: archivio vuoto
        a.scrivi(&dati()).unwrap();
        assert_eq!(a.leggi().unwrap(), dati());
        // sul disco non c'e' nulla in chiaro, e nessun file temporaneo rimasto
        let grezzo = fs::read(c.join(FILE)).unwrap();
        let testo = String::from_utf8_lossy(&grezzo);
        assert!(grezzo.starts_with(INTESTAZIONE) && !testo.contains("RSSMRA80A01L219X") && !testo.contains("Prova"));
        assert!(!c.join("ricette.tmp").exists());
        // due salvataggi uguali sono diversi sul disco (nonce nuovo ogni volta)
        a.scrivi(&dati()).unwrap();
        assert_ne!(fs::read(c.join(FILE)).unwrap(), grezzo);
        // con un'altra chiave non si legge, e un file rovinato non manda in crisi
        assert!(matches!(Archivio::con_chiave(c.join(FILE), nuova_chiave()).leggi_dati(), Err(Lettura::Chiave)));
        fs::write(c.join(FILE), b"CUP1corto").unwrap();
        assert!(matches!(a.leggi_dati(), Err(Lettura::Rovinato)));
        fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn portachiavi_che_non_risponde_non_fa_perdere_le_ricette() {
        let c = cartella("portachiavi");
        let k = nuova_chiave();
        // la prima volta il portachiavi risponde: le ricette si salvano con la sua chiave
        let a = Archivio::apri_con(&c, || Ok(k)).unwrap();
        assert_eq!(a.protezione, Protezione::Portachiavi);
        a.scrivi(&dati()).unwrap();
        // poi non risponde (bloccato, lento): l'app si ferma, niente chiave nuova, niente messo da parte
        let e = Archivio::apri_con(&c, || Err("bloccato".into())).err().unwrap();
        assert!(e.contains("non risponde") && e.contains("bloccato"), "{e}");
        assert!(!c.join(FILE_CHIAVE).exists() && c.join(FILE).exists());
        // quando torna, tutto come prima
        assert_eq!(Archivio::apri_con(&c, || Ok(k)).unwrap().leggi().unwrap(), dati());
        fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn senza_portachiavi_e_senza_ricette_la_chiave_va_in_un_file() {
        let c = cartella("senza");
        let a = Archivio::apri_con(&c, || Err("nessun Secret Service".into())).unwrap();
        assert_eq!(a.protezione, Protezione::File);
        a.scrivi(&dati()).unwrap();
        // dopo, il file vale per primo: il portachiavi non si interroga piu' (le ricette sono cifrate con quella)
        let b = Archivio::apri_con(&c, || panic!("il portachiavi non va interrogato")).unwrap();
        assert_eq!((b.protezione, b.leggi().unwrap()), (Protezione::File, dati()));
        fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn chiave_diversa_o_archivio_rovinato_messi_da_parte() {
        let c = cartella("parte");
        Archivio::apri_con(&c, || Ok(nuova_chiave())).unwrap().scrivi(&dati()).unwrap();
        // portachiavi cancellato: chiave nuova, le ricette vecchie si mettono da parte (non si cancellano)
        let b = Archivio::apri_con(&c, || Ok(nuova_chiave())).unwrap();
        let parte = b.messo_da_parte.clone().unwrap();
        assert!(parte.exists() && b.leggi().unwrap() == Dati::default() && !c.join(FILE).exists());
        // un archivio di una versione piu' nuova (si decifra ma non si capisce) non si tocca
        let k = nuova_chiave();
        let a = Archivio::con_chiave(c.join(FILE), k);
        let nonce = XChaCha20Poly1305::generate_nonce(&mut OsRng);
        let cifrato = XChaCha20Poly1305::new(&k).encrypt(&nonce, b"{\"versione\":9}".as_slice()).unwrap();
        fs::write(c.join(FILE), [INTESTAZIONE, nonce.as_slice(), &cifrato].concat()).unwrap();
        assert!(matches!(a.leggi_dati(), Err(Lettura::Formato(_))));
        let e = Archivio::apri_con(&c, || Ok(k)).err().unwrap();
        assert!(e.contains("altra versione") && c.join(FILE).exists());
        fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn chiave_nel_file_mai_sovrascritta() {
        let c = cartella("file");
        let k = chiave_dal_file(&c.join(FILE_CHIAVE)).unwrap();
        assert_eq!(chiave_dal_file(&c.join(FILE_CHIAVE)).unwrap(), k); // la stessa alla seconda apertura
                                                                       // un file della chiave che non si legge (qui: una cartella con quel nome) e' un errore, non una chiave nuova
        let d = cartella("file-illeggibile");
        fs::create_dir(d.join(FILE_CHIAVE)).unwrap();
        assert!(chiave_dal_file(&d.join(FILE_CHIAVE)).unwrap_err().contains("Non riesco a leggere"));
        fs::write(c.join(FILE_CHIAVE), "non esadecimale").unwrap();
        assert!(chiave_dal_file(&c.join(FILE_CHIAVE)).unwrap_err().contains("rovinato"));
        fs::remove_dir_all(&c).unwrap();
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn eliminata_e_riaggiunta_ricorda_l_ultimo_controllo() {
        let mut d = dati();
        d.ricette[0].ultimo_tentativo = Some(1000);
        d.elimina(1, 1100, 1200);
        assert!(d.ricette.is_empty());
        assert_eq!(d.ultimo_eliminata("010A00000000001", 1100, 1200), Some(1000));
        assert_eq!(d.ultimo_eliminata("010A00000000002", 1100, 1200), None);
        // passati i 20 minuti non serve piu', e si dimentica
        assert_eq!(d.ultimo_eliminata("010A00000000001", 2200, 1200), None);
        assert!(d.eliminate.is_empty());
        // una ricetta mai controllata non lascia traccia
        let mut d = dati();
        d.elimina(1, 1100, 1200);
        assert!(d.eliminate.is_empty());
    }

    #[test]
    fn chiave_esadecimale() {
        let k = nuova_chiave();
        assert_eq!(da_esadecimale(&esadecimale(&k)), Some(k));
        assert_eq!(da_esadecimale("abc"), None);
        assert_eq!(da_esadecimale(&"zz".repeat(32)), None);
    }
}
