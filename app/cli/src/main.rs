//! `cup attuale`: la prenotazione attuale di una ricetta, letta dal portale.
//! Codice fiscale e numero ricetta da CUP_CF e CUP_NRE, o chiesti a terminale: mai come argomenti, che
//! restano nella cronologia dei comandi.

use std::io::{self, BufRead, Write};
use std::process::ExitCode;

use cup_core::analisi::breve;
use cup_core::Sessione;

fn valore(variabile: &str, domanda: &str) -> String {
    if let Ok(v) = std::env::var(variabile) {
        return v;
    }
    print!("{domanda}: ");
    io::stdout().flush().ok();
    let mut riga = String::new();
    io::stdin().lock().read_line(&mut riga).ok();
    riga.trim().to_string()
}

fn main() -> ExitCode {
    if std::env::args().nth(1).as_deref() != Some("attuale") {
        eprintln!("Uso: cup attuale   (codice fiscale e ricetta da CUP_CF e CUP_NRE, o chiesti qui)");
        return ExitCode::from(2);
    }
    let cf = valore("CUP_CF", "Codice fiscale");
    let nre = valore("CUP_NRE", "Numero ricetta");
    let esito = Sessione::nuova(&cf, &nre).and_then(|s| s.elenco());
    match esito {
        Ok(e) => {
            println!("Prenotazione attuale: {}", breve(&e.attuale.quando));
            println!("  {}", e.attuale.luogo);
            println!("  {}", e.attuale.cosa);
            if e.n_prenotate > 1 {
                println!("Righe prenotate: {}", e.n_prenotate);
                for p in &e.prenotate {
                    println!("  - {} | {} | {}", breve(&p.quando), p.luogo.sede, p.cosa);
                }
            }
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("Errore: {e}");
            ExitCode::FAILURE
        }
    }
}
