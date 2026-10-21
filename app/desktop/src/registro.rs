//! Il registro dell'app: un file di testo sul computer con cosa e' successo e quando (avvio, controlli, durate,
//! errori, notifiche). Mai codice fiscale, numero ricetta o prestazione: le ricette compaiono con il loro numero
//! nell'app ("ricetta 3"). Non va da nessuna parte: si legge dalle impostazioni.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// Oltre questa dimensione il registro diventa "cup.log.1" (il precedente si cancella) e se ne comincia uno nuovo.
const MASSIMO: u64 = 512 * 1024;

pub struct Registro {
    file: PathBuf,
    scrittura: Mutex<()>,
}

impl Registro {
    pub fn new(cartella: &Path) -> Self {
        let _ = fs::create_dir_all(cartella);
        Registro { file: cartella.join("cup.log"), scrittura: Mutex::new(()) }
    }

    pub fn percorso(&self) -> &Path {
        &self.file
    }

    /// Una riga con data e ora. Un registro che non si scrive non deve fermare l'app: gli errori si ignorano.
    pub fn scrivi(&self, testo: &str) {
        let _g = self.scrittura.lock().unwrap_or_else(|e| e.into_inner());
        if fs::metadata(&self.file).is_ok_and(|m| m.len() > MASSIMO) {
            let _ = fs::rename(&self.file, self.file.with_extension("log.1"));
        }
        let riga = format!("{} {}\n", chrono::Local::now().format("%Y-%m-%d %H:%M:%S"), testo.replace('\n', " / "));
        let mut o = OpenOptions::new();
        o.create(true).append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            o.mode(0o600);
        }
        if let Ok(mut f) = o.open(&self.file) {
            let _ = f.write_all(riga.as_bytes());
        }
    }

    /// Le ultime `n` righe, dalla piu' vecchia.
    pub fn ultime(&self, n: usize) -> Vec<String> {
        let testo = fs::read_to_string(&self.file).unwrap_or_default();
        let righe: Vec<&str> = testo.lines().collect();
        righe[righe.len().saturating_sub(n)..].iter().map(|s| s.to_string()).collect()
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn righe_con_ora_ultime_e_rotazione() {
        let c = std::env::temp_dir().join(format!("cup-test-registro-{}", std::process::id()));
        let _ = fs::remove_dir_all(&c);
        let r = Registro::new(&c);
        assert!(r.ultime(5).is_empty());
        r.scrivi("avvio");
        r.scrivi("ricetta 1: 3 date\nseconda riga");
        let u = r.ultime(5);
        assert_eq!(u.len(), 2);
        assert!(u[0].ends_with(" avvio") && u[0].len() == "2026-10-21 14:05:03 avvio".len());
        assert!(u[1].ends_with("ricetta 1: 3 date / seconda riga")); // una riga per evento
        assert_eq!(r.ultime(1), vec![u[1].clone()]);
        // oltre il massimo si ruota: il vecchio diventa .1 e si riparte
        fs::write(r.percorso(), vec![b'x'; MASSIMO as usize + 1]).unwrap();
        r.scrivi("dopo la rotazione");
        assert!(c.join("cup.log.1").exists());
        assert_eq!(r.ultime(5).len(), 1);
        fs::remove_dir_all(&c).unwrap();
    }
}
