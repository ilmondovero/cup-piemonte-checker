//! Numero della ricetta e codice fiscale negli appunti, per cercarla sul sito del CUP senza ricopiarli.
//!
//! Il valore lo prende l'app dall'archivio: l'interfaccia vede solo quelli mascherati e chiede "copia la ricetta
//! 3". Su Windows non entra nella cronologia degli appunti (Win+V) ne' negli appunti sincronizzati nel cloud.
//! Dopo DURATA, o alla chiusura dell'app se viene prima, se negli appunti c'e' ancora quello si svuotano.

use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use arboard::Clipboard;

use crate::archivio::Ricetta;
use crate::controlli::blocca;

pub const DURATA: Duration = Duration::from_secs(120);

struct Appunti {
    /// Uno solo per tutta l'app: su Linux gli appunti restano finche' l'oggetto vive.
    c: Clipboard,
    /// L'ultima copia ancora da svuotare: il numero (ogni copia annulla la cancellazione di quella prima) e cosa.
    copiato: Option<(u64, String)>,
    copie: u64,
}

static APPUNTI: Mutex<Option<Appunti>> = Mutex::new(None);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cosa {
    Ricetta,
    CodiceFiscale,
}

impl Cosa {
    pub fn da(s: &str) -> Option<Cosa> {
        match s {
            "nre" => Some(Cosa::Ricetta),
            "cf" => Some(Cosa::CodiceFiscale),
            _ => None,
        }
    }

    pub fn di(self, r: &Ricetta) -> &str {
        match self {
            Cosa::Ricetta => &r.nre,
            Cosa::CodiceFiscale => &r.cf,
        }
    }
}

fn scrivi(c: &mut Clipboard, testo: &str) -> Result<(), arboard::Error> {
    #[cfg(windows)]
    {
        use arboard::SetExtWindows;
        c.set().exclude_from_history().exclude_from_cloud().text(testo)
    }
    #[cfg(not(windows))]
    c.set_text(testo)
}

/// Copia e programma la cancellazione. L'errore non contiene mai il valore.
pub fn copia(testo: &str) -> Result<(), String> {
    let mut g = blocca(&APPUNTI);
    if g.is_none() {
        let c = Clipboard::new().map_err(|_| "Gli appunti del sistema non sono disponibili.".to_string())?;
        *g = Some(Appunti { c, copiato: None, copie: 0 });
    }
    let a = g.as_mut().expect("appena creati");
    scrivi(&mut a.c, testo).map_err(|_| "Copia non riuscita: riprova.".to_string())?;
    a.copie += 1;
    let questa = a.copie;
    a.copiato = Some((questa, testo.to_string()));
    drop(g);
    thread::spawn(move || {
        thread::sleep(DURATA);
        svuota(Some(questa));
    });
    Ok(())
}

/// Svuota gli appunti se contengono ancora l'ultima copia (con `solo`, se l'ultima e' ancora quella).
fn svuota(solo: Option<u64>) {
    let mut g = blocca(&APPUNTI);
    let Some(a) = g.as_mut() else { return };
    let Some((n, testo)) = &a.copiato else { return };
    if solo.is_some_and(|s| s != *n) {
        return;
    }
    // solo se l'utente non ci ha messo altro nel frattempo
    if a.c.get_text().is_ok_and(|t| t == *testo) {
        let _ = a.c.clear();
    }
    a.copiato = None;
}

/// Alla chiusura dell'app: non restano negli appunti dopo.
pub fn alla_chiusura() {
    svuota(None);
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn cosa_si_copia() {
        let r = crate::archivio::test::ricetta();
        assert_eq!(Cosa::da("nre").unwrap().di(&r), r.nre);
        assert_eq!(Cosa::da("cf").unwrap().di(&r), r.cf);
        assert_eq!(Cosa::da("email"), None);
        assert_eq!(Cosa::da(""), None);
    }
}
