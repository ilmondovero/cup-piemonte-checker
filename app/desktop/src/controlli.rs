//! I controlli automatici: ogni ricetta attiva al massimo una volta ogni 20 minuti (ogni controllo apre "Sposta",
//! o gli appuntamenti di una ricetta mai prenotata, e blocca una data per ~40 minuti), una ricetta alla volta, in un
//! thread a parte.

use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use cup_core::analisi::breve;
use cup_core::{Calendario, Controllo, Dove, Errore, Sessione, Slot, Zona};
use serde::Serialize;

use crate::archivio::{Archivio, Data, Dati, Esito, Ricetta, Visita, NOTIFICATE_MAX};
use crate::registro::Registro;

/// Secondi tra due controlli della stessa ricetta: come il bot.
pub const INTERVALLO: i64 = 20 * 60;
/// Il portale a volte non trova una prenotazione che c'e': si sospende solo dopo tante volte di fila.
pub const ASSENZE_PAUSA: u32 = 3;
/// Date nuove scritte per intero in una notifica (le altre si vedono nell'app).
const NOTIFICA_DATE: usize = 3;
/// Ogni quanto il thread guarda se c'e' un controllo da fare.
const GIRO: Duration = Duration::from_secs(5);
/// Per quanto la sessione di un controllo si tiene per uno spostamento: il portale tiene la proposta ~40 minuti.
pub const TENUTA: i64 = 35 * 60;

/// Cosa si sta facendo sul portale per una ricetta: una cosa alla volta.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Attivita {
    Controllo,
    Spostamento,
    Prenotazione,
    Disdetta,
}

impl Attivita {
    /// Uno spostamento, una prenotazione o una disdetta: a meta' l'esito resterebbe da verificare a mano.
    pub fn operazione(self) -> bool {
        self != Attivita::Controllo
    }
}

/// La sessione del controllo che ha trovato date buone: "Sposta" e' aperto e la proposta e' tenuta per noi.
pub struct Tenuta {
    pub sessione: Sessione,
    pub ts: i64,
    /// Le impostazioni della ricetta al momento del controllo: se cambiano, la sessione non vale piu'.
    pub impostazioni: Impostazioni,
}

pub struct Stato {
    pub archivio: Archivio,
    pub registro: Registro,
    pub dati: Mutex<Dati>,
    /// Le ricette su cui c'e' qualcosa in corso. Si prende prima di `dati`, mai dopo.
    pub in_corso: Mutex<HashMap<u64, Attivita>>,
    /// Le sessioni dei controlli, per spostare sulle date appena trovate.
    pub tenute: Mutex<HashMap<u64, Tenuta>>,
    /// L'ultimo salvataggio fallito, da mostrare nell'interfaccia (None se l'ultimo e' riuscito).
    pub avviso: Mutex<Option<String>>,
    /// L'app si sta chiudendo per installare un aggiornamento: non parte piu' nessun controllo e nessuna operazione.
    chiusura: AtomicBool,
}

pub fn blocca<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    // un thread caduto tenendo il lock non deve bloccare l'app: i dati restano quelli di prima
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl Stato {
    pub fn new(archivio: Archivio, registro: Registro, dati: Dati) -> Self {
        Stato {
            archivio,
            registro,
            dati: Mutex::new(dati),
            in_corso: Mutex::new(HashMap::new()),
            tenute: Mutex::new(HashMap::new()),
            avviso: Mutex::new(None),
            chiusura: AtomicBool::new(false),
        }
    }

    /// Per installare un aggiornamento: se non c'e' niente in corso, da qui in poi non parte piu' nulla (true).
    /// Si decide con `in_corso` bloccato, lo stesso lock con cui partono controlli e operazioni.
    pub fn chiudi_per_aggiornare(&self) -> bool {
        let in_corso = blocca(&self.in_corso);
        if !in_corso.is_empty() {
            return false;
        }
        self.chiusura.store(true, Ordering::SeqCst);
        true
    }

    /// L'aggiornamento non si e' installato: controlli e operazioni ripartono.
    pub fn riapri(&self) {
        self.chiusura.store(false, Ordering::SeqCst);
    }

    pub fn in_chiusura(&self) -> bool {
        self.chiusura.load(Ordering::SeqCst)
    }

    pub fn dati(&self) -> MutexGuard<'_, Dati> {
        blocca(&self.dati)
    }

    pub fn in_corso(&self) -> HashMap<u64, Attivita> {
        blocca(&self.in_corso).clone()
    }

    /// Finita l'attivita' sulla ricetta.
    pub fn libera(&self, id: u64) {
        blocca(&self.in_corso).remove(&id);
    }

    pub fn avviso(&self) -> Option<String> {
        blocca(&self.avviso).clone()
    }

    /// Salva; un errore resta visibile nell'interfaccia e nel registro finche' un salvataggio non riesce.
    pub fn salva(&self, dati: &Dati) -> Result<(), String> {
        let esito = self.archivio.scrivi(dati);
        if let Err(e) = &esito {
            self.registro.scrivi(&format!("salvataggio non riuscito: {e}"));
        }
        *blocca(&self.avviso) = esito.as_ref().err().map(|e| format!("{e}. Le ultime modifiche non sono salvate."));
        esito
    }
}

pub fn adesso() -> i64 {
    chrono::Utc::now().timestamp()
}

/// Il controllo della ricetta va fatto adesso? (Un orario nel futuro, dopo che l'orologio e' tornato indietro,
/// vale come "adesso": si aspettano 20 minuti da ora.)
pub fn dovuta(r: &Ricetta, adesso: i64) -> bool {
    r.attiva && r.ultimo_tentativo.is_none_or(|t| adesso - t.min(adesso) >= INTERVALLO)
}

/// Quando si potra' fare il prossimo controllo (secondi dal 1970), se la ricetta e' attiva.
pub fn prossimo(r: &Ricetta, adesso: i64) -> Option<i64> {
    r.attiva.then(|| r.ultimo_tentativo.map_or(adesso, |t| (t.min(adesso) + INTERVALLO).max(adesso)))
}

/// Una zona "della prenotazione" (sede, comune o provincia senza un nome).
pub fn della_prenotazione(dove: &str, valore: &str) -> bool {
    matches!(dove, "sede" | "comune" | "provincia") && valore.trim().is_empty()
}

/// La zona e i giorni da evitare di una ricetta.
pub fn zona_di(r: &Ricetta) -> Result<(Zona, Calendario), Errore> {
    // senza prenotazione una zona "della prenotazione" non vuol dire nulla: finche' non e' prenotata si cerca dove
    // propone il CUP, poi torna a valere la scelta dell'utente
    let dove = if r.nuova && della_prenotazione(&r.dove, &r.valore) {
        Dove::Tutte
    } else {
        Dove::da_scelta(&r.dove, &r.valore)?
    };
    let zona = Zona::new(dove);
    Ok((zona, Calendario { no_settimana: r.evita.iter().copied().filter(|g| *g < 7).collect(), ..Default::default() }))
}

/// Un messaggio "Il portale ha risposto: <testo del portale>" senza il testo del portale, che puo' nominare la
/// ricetta o la prestazione. Resta intera solo la descrizione di una pagina sconosciuta (`analisi::struttura`), che
/// ha solo nomi di sezioni e pulsanti.
fn senza_testo_del_portale(m: &str) -> &str {
    match m.split_once(": ") {
        Some((_, dopo)) if dopo.starts_with(cup_core::analisi::STRUTTURA) => m,
        Some((prima, _)) => prima,
        None => m,
    }
}

/// L'errore come va nel registro: il tipo, senza testi che possono contenere la prestazione o le date.
pub fn tipo(e: &Errore) -> String {
    match e {
        Errore::NonTrovata(_) => "il portale non trova la ricetta o la prenotazione".into(),
        Errore::GiaPrenotata(_) => "la ricetta risulta già prenotata".into(),
        Errore::NonAttiva { stati } => format!("prenotazione non attiva ({})", stati.join(", ")),
        Errore::Doppione(_) => "due prenotazioni della stessa prestazione".into(),
        Errore::DatiNonValidi(_) => "dati della ricetta non validi".into(),
        Errore::Timeout => "timeout".into(),
        Errore::Sovraccarico(m) => format!("portale sovraccarico ({m})"),
        Errore::Inattesa(m) => format!("risposta inattesa ({})", senza_testo_del_portale(m)),
        Errore::Incerto(_) => "esito incerto".into(),
    }
}

#[derive(Debug)]
pub struct Notifica {
    /// Per il registro, che non scrive titolo e testo (nome scelto dall'utente, date della visita).
    pub ricetta: u64,
    pub tipo: &'static str,
    pub titolo: String,
    pub testo: String,
}

fn data(s: &Slot) -> Data {
    Data { chiave: s.chiave(), quando: breve(&s.quando), luogo: s.luogo.to_string(), proposta: s.proposta }
}

/// Aggiorna la ricetta con l'esito di un controllo: prenotazione, date buone, errori, pausa. Restituisce le notifiche
/// da mostrare: le date buone mai notificate prima (sempre con data, ora e luogo) e le pause decise qui.
pub fn applica(r: &mut Ricetta, esito: Result<Controllo, Errore>, zona: &Zona, ts: i64) -> Vec<Notifica> {
    let prima = r.esito.take().unwrap_or_default();
    match esito {
        Ok(c) => {
            let a = c.elenco.as_ref().map(|e| &e.attuale);
            let buone: Vec<Data> = c.migliori.iter().map(data).collect();
            let mut nuove: Vec<&Data> = vec![];
            for d in &buone {
                if !r.notificate.contains(&d.chiave) && !nuove.iter().any(|x| x.chiave == d.chiave) {
                    nuove.push(d);
                }
            }
            let mut notifiche = vec![];
            if !nuove.is_empty() {
                let mut righe: Vec<String> =
                    nuove.iter().take(NOTIFICA_DATE).map(|d| format!("{} · {}", d.quando, d.luogo)).collect();
                match nuove.len().saturating_sub(NOTIFICA_DATE) {
                    0 => {}
                    1 => righe.push("e un'altra data: la vedi nell'app".into()),
                    n => righe.push(format!("e altre {n} date: le vedi nell'app")),
                }
                // senza prenotazione ogni data nella zona va bene: e' un posto libero, non una data "prima"
                let titolo = match (a.is_some(), nuove.len()) {
                    (true, 1) => "Data prima",
                    (true, _) => "Date prima",
                    (false, 1) => "Posto libero",
                    (false, _) => "Posti liberi",
                };
                notifiche.push(Notifica {
                    ricetta: r.id,
                    tipo: "date nuove",
                    titolo: format!("{titolo} per {}", r.nome),
                    testo: righe.join("\n"),
                });
            }
            r.notificate.extend(nuove.iter().map(|d| d.chiave.clone()));
            let troppe = r.notificate.len().saturating_sub(NOTIFICATE_MAX);
            r.notificate.drain(..troppe);
            r.assenze = 0;
            let cosa = match (a, c.prestazioni.is_empty()) {
                (None, false) => c.prestazioni.join(" + "),
                (None, true) => prima.cosa,
                (Some(_), _) => String::new(),
            };
            r.esito = Some(Esito {
                ts,
                controllo: true,
                attuale: a.map(Visita::da),
                zona: zona.dove.descrizione(a),
                date: c.slots.len(),
                buone,
                area_incompleta: c.area_incompleta,
                errore: None,
                nota: None,
                note_visita: prima.note_visita,
                cosa,
                solo_proposta: c.solo_proposta,
            });
            notifiche
        }
        // la ricetta seguita come mai prenotata e' stata prenotata (dal sito del CUP, al telefono...): da ora si
        // cercano date prima della prenotazione
        Err(Errore::GiaPrenotata(m)) => {
            r.nuova = false;
            r.assenze = 0;
            r.esito = Some(Esito {
                ts,
                controllo: false,
                errore: None,
                buone: vec![],
                date: 0,
                nota: Some(format!("{m}: al prossimo controllo l'app legge la prenotazione e cerca date prima.")),
                ..prima
            });
            vec![Notifica {
                ricetta: r.id,
                tipo: "già prenotata",
                titolo: format!("{}: la ricetta risulta prenotata", r.nome),
                testo: format!("{m}. Da ora l'app cerca date prima della prenotazione."),
            }]
        }
        Err(e) => {
            let pausa = match &e {
                Errore::NonTrovata(_) => {
                    r.assenze += 1;
                    (r.assenze >= ASSENZE_PAUSA).then(|| {
                        if r.nuova {
                            format!("Il portale non accetta la ricetta ({e})")
                        } else {
                            format!("Il portale non trova più la prenotazione ({e})")
                        }
                    })
                }
                Errore::NonAttiva { .. } | Errore::Doppione(_) | Errore::DatiNonValidi(_) => Some(e.to_string()),
                // portale lento, sovraccarico o cambiato: si riprova al prossimo giro
                Errore::Timeout
                | Errore::Sovraccarico(_)
                | Errore::Inattesa(_)
                | Errore::Incerto(_)
                | Errore::GiaPrenotata(_) => None,
            };
            r.esito = Some(Esito {
                ts,
                controllo: true,
                errore: Some(e.to_string()),
                buone: vec![],
                date: 0,
                nota: None,
                ..prima
            });
            match pausa {
                Some(motivo) => {
                    r.attiva = false;
                    vec![Notifica {
                        ricetta: r.id,
                        tipo: "pausa",
                        titolo: format!("{}: controlli in pausa", r.nome),
                        testo: format!("{motivo}. Riprendili dall'app quando vuoi."),
                    }]
                }
                None => vec![],
            }
        }
    }
}

pub type Impostazioni = (String, String, String, String, Vec<u32>);

/// Le impostazioni da cui dipende un controllo: se cambiano mentre e' in corso, il suo esito non vale piu'.
pub fn impostazioni(r: &Ricetta) -> Impostazioni {
    (r.cf.clone(), r.nre.clone(), r.dove.clone(), r.valore.clone(), r.evita.clone())
}

/// L'esito di un controllo e la sua sessione (con "Sposta" aperto), se c'e'.
pub type Risultato = Result<(Controllo, Option<Sessione>), Errore>;

/// Un controllo vero sul portale: "Sposta" per una ricetta prenotata, Ricerca e Prestazioni per una mai prenotata.
pub fn sul_portale(r: &Ricetta, zona: &Zona, cal: &Calendario) -> Risultato {
    let mut s = Sessione::nuova(&r.cf, &r.nre)?;
    if !r.nuova {
        let c = s.controlla(zona, cal)?;
        return Ok((c, Some(s)));
    }
    match s.controlla_nuova(zona, cal) {
        Ok(c) => Ok((c, Some(s))),
        // la Ricerca dice solo "gia' presente": data, ora e luogo si leggono dall'elenco (solo lettura)
        Err(Errore::GiaPrenotata(m)) => {
            Err(Errore::GiaPrenotata(match Sessione::nuova(&r.cf, &r.nre).and_then(|mut x| x.elenco()) {
                Ok(e) => format!("La ricetta risulta prenotata al {} · {}", breve(&e.attuale.quando), e.attuale.luogo),
                Err(_) => format!("Il portale dice che la ricetta è già prenotata ({m})"),
            }))
        }
        Err(e) => Err(e),
    }
}

/// Un giro: se c'e' una ricetta da controllare la controlla con `controlla` e ne salva l'esito. `inizio` si chiama
/// quando il controllo parte (per mostrarlo nell'interfaccia). None se non c'era niente da fare.
pub fn giro(
    stato: &Stato,
    controlla: &dyn Fn(&Ricetta, &Zona, &Calendario) -> Risultato,
    inizio: &dyn Fn(),
) -> Option<Vec<Notifica>> {
    let r = {
        let mut in_corso = blocca(&stato.in_corso);
        if stato.in_chiusura() {
            return None; // si sta installando un aggiornamento
        }
        let mut d = stato.dati();
        let ora = adesso();
        // una ricetta che si sta spostando o disdicendo aspetta il giro dopo
        let r = d.ricette.iter_mut().find(|r| dovuta(r, ora) && !in_corso.contains_key(&r.id))?;
        r.ultimo_tentativo = Some(ora); // da qui si contano i 20 minuti, anche se il controllo fallisce
        let r = r.clone();
        // senza l'orario salvato, dopo un riavvio si ricontrollerebbe prima dei 20 minuti: meglio non partire
        // (in memoria l'orario resta, quindi il prossimo tentativo e' tra 20 minuti)
        stato.salva(&d).ok()?;
        in_corso.insert(r.id, Attivita::Controllo);
        r
    };
    inizio();
    stato.registro.scrivi(&format!("ricetta {}: controllo", r.id));
    let partito = Instant::now();
    // "Sposta" si apre all'inizio del controllo: l'eta' della sessione si conta da qui
    let partito_ts = adesso();
    let esito = zona_di(&r).map(|(zona, cal)| {
        // un errore imprevisto nel motore non deve fermare i controlli: vale come una risposta inattesa
        let c = catch_unwind(AssertUnwindSafe(|| controlla(&r, &zona, &cal)))
            .unwrap_or_else(|_| Err(Errore::Inattesa("errore interno del controllo".into())));
        (zona, c)
    });
    let secondi = partito.elapsed().as_secs();
    let (zona, esito) = match esito {
        Ok((zona, Ok((c, sessione)))) => {
            // la sessione si tiene solo se ha trovato date da proporre; quella di prima non vale piu'
            let mut tenute = blocca(&stato.tenute);
            tenute.remove(&r.id);
            if let Some(sessione) = sessione.filter(|_| !c.migliori.is_empty()) {
                tenute.insert(r.id, Tenuta { sessione, ts: partito_ts, impostazioni: impostazioni(&r) });
            }
            (zona, Ok(c))
        }
        Ok((zona, Err(e))) => (zona, Err(e)),
        Err(e) => (Zona::new(Dove::Tutte), Err(e)),
    };
    stato.registro.scrivi(&match &esito {
        Ok(c) => format!(
            "ricetta {}: {} date offerte, {} buone, {secondi} s{}",
            r.id,
            c.slots.len(),
            c.migliori.len(),
            if c.area_incompleta { ", area estesa incompleta" } else { "" }
        ),
        Err(e) => format!("ricetta {}: {} dopo {secondi} s", r.id, tipo(e)),
    });
    let notifiche = {
        let mut d = stato.dati();
        // la ricetta puo' essere stata cancellata o cambiata nel frattempo: allora l'esito non vale
        let x = d.ricette.iter_mut().find(|x| x.id == r.id && impostazioni(x) == impostazioni(&r));
        let notifiche = match x {
            Some(x) => {
                let attiva = x.attiva;
                let n = applica(x, esito, &zona, adesso());
                if attiva && !x.attiva {
                    stato.registro.scrivi(&format!("ricetta {}: controlli in pausa", r.id));
                }
                let _ = stato.salva(&d);
                n
            }
            None => {
                blocca(&stato.tenute).remove(&r.id); // la sessione di una ricetta cambiata o eliminata non serve
                stato.registro.scrivi(&format!("ricetta {}: cambiata o eliminata durante il controllo", r.id));
                vec![]
            }
        };
        notifiche
    };
    stato.libera(r.id);
    Some(notifiche)
}

/// Avvia il thread dei controlli. `avvisa` mostra le notifiche, `aggiornato` dice all'interfaccia di ridisegnarsi.
pub fn avvia(
    stato: Arc<Stato>,
    avvisa: impl Fn(Vec<Notifica>) + Send + 'static,
    aggiornato: impl Fn() + Send + 'static,
) {
    thread::spawn(move || loop {
        thread::sleep(GIRO);
        // un errore imprevisto fuori dal controllo non deve fermare il thread: si riparte al giro dopo
        match catch_unwind(AssertUnwindSafe(|| giro(&stato, &sul_portale, &aggiornato))) {
            Ok(Some(notifiche)) => {
                avvisa(notifiche);
                aggiornato();
            }
            Ok(None) => {}
            Err(_) => {
                // solo i controlli: uno spostamento in corso nell'altro thread resta segnato
                blocca(&stato.in_corso).retain(|_, a| *a != Attivita::Controllo);
                stato.registro.scrivi("errore interno nel giro dei controlli");
                aggiornato();
            }
        }
    });
}

#[cfg(test)]
mod test {
    use std::cell::Cell;
    use std::path::PathBuf;

    use chacha20poly1305::aead::{KeyInit, OsRng};
    use chacha20poly1305::XChaCha20Poly1305;
    use cup_core::{Elenco, Luogo, Prenotazione};

    use super::*;

    fn ricetta() -> Ricetta {
        Ricetta {
            id: 1,
            nome: "Genetica".into(),
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
            operazione: None,
            incerta: None,
            nuova: false,
        }
    }

    fn quando(g: u32, h: u32) -> chrono::NaiveDateTime {
        chrono::NaiveDate::from_ymd_opt(2026, 12, g).unwrap().and_hms_opt(h, 0, 0).unwrap()
    }

    fn slot(g: u32, sede: &str) -> Slot {
        Slot {
            quando: quando(g, 9),
            luogo: Luogo { sede: sede.into(), ambulatorio: "AMB".into(), indirizzo: "- TORINO (TO)".into() },
            seleziona: Some("s".into()),
            proposta: false,
        }
    }

    fn controllo(migliori: Vec<Slot>) -> Controllo {
        let attuale = Prenotazione {
            quando: quando(31, 11),
            luogo: Luogo { sede: "MOLINETTE".into(), ambulatorio: "AMB".into(), indirizzo: "- TORINO (TO)".into() },
            cosa: "VISITA".into(),
        };
        Controllo {
            elenco: Some(Elenco {
                attuale: attuale.clone(),
                prenotate: vec![attuale],
                righe: vec![0],
                n_prenotate: 1,
                riga: 0,
            }),
            prestazioni: vec![],
            slots: migliori.clone(),
            migliori,
            area_incompleta: false,
            solo_proposta: false,
        }
    }

    fn stato(nome: &str, ricette: Vec<Ricetta>) -> (Stato, PathBuf) {
        let c = std::env::temp_dir().join(format!("cup-test-giro-{nome}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&c);
        std::fs::create_dir_all(&c).unwrap();
        let archivio = Archivio::con_chiave(c.join("ricette.cifrate"), XChaCha20Poly1305::generate_key(&mut OsRng));
        let dati = Dati { prossimo_id: 9, ricette, eliminate: vec![] };
        (Stato::new(archivio, Registro::new(&c), dati), c)
    }

    #[test]
    fn zona_della_prenotazione_senza_prenotazione() {
        let mut r = ricetta();
        (r.dove, r.valore, r.nuova) = ("comune".into(), String::new(), true);
        assert_eq!(zona_di(&r).unwrap().0.dove, Dove::Tutte);
        // prenotata: torna a valere la scelta dell'utente
        r.nuova = false;
        assert_eq!(zona_di(&r).unwrap().0.dove, Dove::Comune(String::new()));
        // un comune scritto vale sempre
        (r.valore, r.nuova) = ("Alba".into(), true);
        assert_eq!(zona_di(&r).unwrap().0.dove, Dove::Comune("ALBA".into()));
    }

    #[test]
    fn ogni_venti_minuti_solo_se_attiva() {
        let mut r = ricetta();
        assert!(dovuta(&r, 1000) && prossimo(&r, 1000) == Some(1000));
        r.ultimo_tentativo = Some(1000);
        assert!(!dovuta(&r, 1000 + INTERVALLO - 1) && dovuta(&r, 1000 + INTERVALLO));
        assert_eq!(prossimo(&r, 1100), Some(1000 + INTERVALLO));
        // l'orologio torna indietro: si aspettano 20 minuti da adesso, non da un orario nel futuro
        assert!(!dovuta(&r, 500) && prossimo(&r, 500) == Some(500 + INTERVALLO));
        r.attiva = false;
        assert!(!dovuta(&r, 1_000_000) && prossimo(&r, 1100).is_none());
    }

    #[test]
    fn date_nuove_notificate_una_volta_con_data_ora_e_luogo() {
        let mut r = ricetta();
        let zona = zona_di(&r).unwrap().0;
        let n = applica(&mut r, Ok(controllo(vec![slot(1, "MAURIZIANO"), slot(2, "CTO")])), &zona, 50);
        assert_eq!(n.len(), 1);
        assert_eq!(n[0].titolo, "Date prima per Genetica");
        assert_eq!(
            n[0].testo,
            "mar 01/12/2026 ore 09:00 · MAURIZIANO, AMB, TORINO (TO)\nmer 02/12/2026 ore 09:00 · CTO, AMB, TORINO (TO)"
        );
        let e = r.esito.clone().unwrap();
        assert_eq!((e.ts, e.date, e.buone.len(), e.zona.as_str()), (50, 2, 2, "solo nel comune di Torino"));
        assert_eq!(e.attuale.unwrap().quando, "gio 31/12/2026 ore 11:00");
        // le stesse date al controllo dopo: niente notifica; una nuova: solo quella
        assert!(applica(&mut r, Ok(controllo(vec![slot(1, "MAURIZIANO"), slot(2, "CTO")])), &zona, 60).is_empty());
        let n = applica(&mut r, Ok(controllo(vec![slot(1, "MAURIZIANO"), slot(3, "OFTALMICO")])), &zona, 70);
        assert_eq!(
            (n[0].titolo.as_str(), n[0].testo.as_str()),
            ("Data prima per Genetica", "gio 03/12/2026 ore 09:00 · OFTALMICO, AMB, TORINO (TO)")
        );
        // tante date nuove: le prime tre per intero; la stessa data due volte conta una volta
        let molte = vec![slot(10, "A"), slot(10, "A"), slot(11, "B"), slot(12, "C"), slot(13, "D"), slot(14, "E")];
        let n = applica(&mut r, Ok(controllo(molte)), &zona, 80);
        let righe: Vec<&str> = n[0].testo.lines().collect();
        assert_eq!(righe.len(), 4);
        assert!(righe[2].starts_with("sab 12/12/2026 ore 09:00 · C") && righe[3] == "e altre 2 date: le vedi nell'app");
        assert_eq!(r.notificate.iter().filter(|k| **k == slot(10, "A").chiave()).count(), 1);
        // l'elenco delle date notificate non cresce all'infinito
        r.notificate = (0..NOTIFICATE_MAX).map(|i| i.to_string()).collect();
        applica(&mut r, Ok(controllo(vec![slot(4, "NUOVO")])), &zona, 90);
        assert_eq!(r.notificate.len(), NOTIFICATE_MAX);
        assert_eq!(r.notificate.last().unwrap(), &slot(4, "NUOVO").chiave());
    }

    #[test]
    fn errori_che_mettono_in_pausa_e_errori_del_portale() {
        let mut r = ricetta();
        let zona = zona_di(&r).unwrap().0;
        applica(&mut r, Ok(controllo(vec![])), &zona, 10);
        // portale lento: si registra e si riprova, la prenotazione letta prima resta
        assert!(applica(&mut r, Err(Errore::Timeout), &zona, 20).is_empty());
        let e = r.esito.clone().unwrap();
        assert!(r.attiva && e.errore.is_some() && e.attuale.is_some() && e.ts == 20);
        // "nessuna prenotazione" puo' essere un errore del portale: pausa solo alla terza di fila
        for _ in 0..2 {
            assert!(applica(&mut r, Err(Errore::NonTrovata("Nessun record".into())), &zona, 30).is_empty());
        }
        assert!(r.attiva);
        let n = applica(&mut r, Err(Errore::NonTrovata("Nessun record".into())), &zona, 40);
        assert!(!r.attiva && n.len() == 1 && n[0].titolo == "Genetica: controlli in pausa");
        // un controllo riuscito azzera il conto
        r.attiva = true;
        r.assenze = 2;
        applica(&mut r, Ok(controllo(vec![])), &zona, 50);
        assert_eq!(r.assenze, 0);
        // disdetta o erogata, doppione: pausa subito
        let n = applica(&mut r, Err(Errore::NonAttiva { stati: vec!["EROGATO".into()] }), &zona, 60);
        assert!(!r.attiva && n[0].testo.contains("EROGATO"));
        r.attiva = true;
        applica(&mut r, Err(Errore::Doppione("due prenotazioni".into())), &zona, 70);
        assert!(!r.attiva);
    }

    #[test]
    fn nel_registro_mai_il_testo_del_portale() {
        let e = |m: &str| tipo(&Errore::Inattesa(m.into()));
        assert_eq!(
            e("Il portale ha risposto: La ricetta 010A00000000001 non è disponibile"),
            "risposta inattesa (Il portale ha risposto)"
        );
        assert_eq!(
            e("Il portale non va avanti: Prestazione ECOGRAFIA non prenotabile"),
            "risposta inattesa (Il portale non va avanti)"
        );
        // la descrizione di una pagina sconosciuta resta: serve a capire il passo, e non ha dati
        let s = "Passo del portale non riconosciuto: sezioni [Prestazioni], moduli [quesitoForm], pulsanti []";
        assert_eq!(e(s), format!("risposta inattesa ({s})"));
        assert_eq!(e("timeout dell'area"), "risposta inattesa (timeout dell'area)");
        assert_eq!(cup_core::analisi::id_breve("x:riga_RSSMRA80A01L219X"), "?");
        assert_eq!(cup_core::analisi::id_breve("x:riga_010A00000000001_b"), "?");
        assert_eq!(cup_core::analisi::id_breve("x:prestazioni-nextButton-main"), "prestazioni-nextButton-main");
    }

    #[test]
    fn ricetta_mai_prenotata_posti_liberi_e_poi_prenotata_altrove() {
        let mut r = ricetta();
        r.nuova = true;
        (r.dove, r.valore) = ("comune".into(), "Torino".into());
        let zona = zona_di(&r).unwrap().0;
        let mut c = controllo(vec![slot(1, "MAURIZIANO")]);
        c.elenco = None;
        c.prestazioni = vec!["ECOGRAFIA".into(), "VISITA".into()];
        c.solo_proposta = true;
        let n = applica(&mut r, Ok(c), &zona, 10);
        assert_eq!(n[0].titolo, "Posto libero per Genetica");
        assert_eq!(n[0].testo, "mar 01/12/2026 ore 09:00 · MAURIZIANO, AMB, TORINO (TO)");
        let e = r.esito.clone().unwrap();
        assert!(e.attuale.is_none() && e.solo_proposta && e.buone.len() == 1);
        assert_eq!((e.cosa.as_str(), e.zona.as_str()), ("ECOGRAFIA + VISITA", "solo nel comune di Torino"));
        // il portale la dice gia' prenotata (dal sito del CUP, al telefono): da ora si cercano date prima, senza pausa
        let m = "La ricetta risulta prenotata al gio 07/01/2027 ore 09:00 · MOLINETTE, AMB, TORINO (TO)";
        let n = applica(&mut r, Err(Errore::GiaPrenotata(m.into())), &zona, 20);
        assert!(!r.nuova && r.attiva && n[0].titolo == "Genetica: la ricetta risulta prenotata");
        assert_eq!(n[0].testo, format!("{m}. Da ora l'app cerca date prima della prenotazione."));
        let e = r.esito.clone().unwrap();
        assert!(e.buone.is_empty() && e.errore.is_none());
        assert_eq!(
            e.nota.unwrap(),
            format!("{m}: al prossimo controllo l'app legge la prenotazione e cerca date prima.")
        );
        // una ricetta che il portale rifiuta va in pausa alla terza volta, con il motivo giusto
        r.nuova = true;
        for _ in 0..ASSENZE_PAUSA {
            applica(&mut r, Err(Errore::NonTrovata("Ricetta scaduta".into())), &zona, 30);
        }
        assert!(!r.attiva);
        assert!(r.esito.unwrap().errore.unwrap().contains("scaduta"));
    }

    #[test]
    fn un_giro_controlla_salva_e_rispetta_i_venti_minuti() {
        let mut pausa = ricetta();
        (pausa.id, pausa.nre, pausa.attiva) = (2, "010A00000000002".into(), false);
        let (s, c) = stato("giro", vec![pausa, ricetta()]);
        let chiamate = Cell::new(0);
        let iniziato = Cell::new(false);
        let controlla = |r: &Ricetta, _: &Zona, _: &Calendario| {
            chiamate.set(chiamate.get() + 1);
            assert_eq!(r.id, 1); // quella in pausa non si controlla
            Ok((controllo(vec![slot(1, "MAURIZIANO")]), None))
        };
        let n = giro(&s, &controlla, &|| iniziato.set(true)).unwrap();
        assert!(iniziato.get() && chiamate.get() == 1 && n.len() == 1);
        assert!(s.in_corso().is_empty());
        // salvato sul disco: esito e orario del controllo
        let d = s.archivio.leggi().unwrap();
        assert!(d.ricette[1].ultimo_tentativo.is_some() && d.ricette[1].esito.as_ref().unwrap().buone.len() == 1);
        // subito dopo non c'e' niente da fare: i 20 minuti valgono anche per il thread
        assert!(giro(&s, &controlla, &|| {}).is_none());
        // con un controllo in corso l'app non si chiude per aggiornarsi; senza, si chiude e non parte piu' nulla
        blocca(&s.in_corso).insert(1, Attivita::Controllo);
        assert!(!s.chiudi_per_aggiornare() && !s.in_chiusura());
        s.libera(1);
        assert!(s.chiudi_per_aggiornare());
        s.dati().ricette[1].ultimo_tentativo = None; // di nuovo dovuta
        assert!(giro(&s, &controlla, &|| {}).is_none());
        s.riapri();
        assert_eq!(chiamate.get(), 1);
        assert!(s.registro.ultime(5).iter().any(|r| r.ends_with("ricetta 1: 1 date offerte, 1 buone, 0 s")));
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn esito_scartato_se_la_ricetta_cambia_e_panico_del_motore() {
        let (s, c) = stato("cambia", vec![ricetta()]);
        // durante il controllo l'utente cambia la zona: l'esito non vale piu'
        let cambia = |_: &Ricetta, _: &Zona, _: &Calendario| {
            s.dati().ricette[0].dove = "tutte".into();
            Ok((controllo(vec![slot(1, "MAURIZIANO")]), None))
        };
        assert!(giro(&s, &cambia, &|| {}).unwrap().is_empty());
        assert!(s.dati().ricette[0].esito.is_none() && s.dati().ricette[0].ultimo_tentativo.is_some());
        // un panic nel motore: il controllo vale come risposta inattesa, e in_corso si azzera
        s.dati().ricette[0].ultimo_tentativo = None;
        let rotto = |_: &Ricetta, _: &Zona, _: &Calendario| -> Risultato { panic!("indice fuori") };
        assert!(giro(&s, &rotto, &|| {}).unwrap().is_empty());
        let e = s.dati().ricette[0].esito.clone().unwrap();
        assert_eq!(e.errore.as_deref(), Some("errore interno del controllo"));
        assert!(s.in_corso().is_empty());
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn senza_salvataggio_il_controllo_non_parte() {
        let (s, c) = stato("nonsalva", vec![ricetta()]);
        std::fs::create_dir(c.join("ricette.cifrate")).unwrap(); // non si puo' scrivere
        let mai = |_: &Ricetta, _: &Zona, _: &Calendario| -> Risultato { panic!("non deve partire") };
        assert!(giro(&s, &mai, &|| {}).is_none());
        assert!(s.avviso().is_some() && s.dati().ricette[0].ultimo_tentativo.is_some());
        std::fs::remove_dir_all(&c).unwrap();
    }

    #[test]
    fn salvataggio_fallito_visibile() {
        let (s, c) = stato("salva", vec![ricetta()]);
        std::fs::create_dir(c.join("ricette.cifrate")).unwrap(); // al posto del file: non si puo' scrivere
        assert!(s.salva(&s.dati()).is_err());
        assert!(s.avviso().unwrap().contains("non sono salvate"));
        std::fs::remove_dir(c.join("ricette.cifrate")).unwrap();
        s.salva(&s.dati()).unwrap();
        assert_eq!(s.avviso(), None);
        std::fs::remove_dir_all(&c).unwrap();
    }
}
