//! Prova del motore da terminale.
//!
//!   cup attuale                       la prenotazione attuale (solo lettura)
//!   cup cerca [opzioni]               le date offerte da "Sposta appuntamento" e quelle che vanno bene
//!     --dove sede|comune|comuni|cintura|provincia|tutte   (predefinito: comune della prenotazione)
//!     --valore TESTO                  il comune, i comuni separati da virgole o la sigla della provincia
//!     --evita ven,sab                 giorni della settimana da evitare
//!     --si                            non chiedere conferma prima di aprire "Sposta"
//!
//! Codice fiscale e numero ricetta da CUP_CF e CUP_NRE, o chiesti a terminale: mai come argomenti, che
//! restano nella cronologia dei comandi.

use std::io::{self, BufRead, Write};
use std::process::ExitCode;

use cup_core::analisi::breve;
use cup_core::zona::{giorno_settimana, perche_no};
use cup_core::{Calendario, Dove, Errore, Sessione, Zona};

const USO: &str = "Uso: cup attuale | cup cerca [--dove TIPO] [--valore TESTO] [--evita ven,sab] [--si]";

fn chiedi(domanda: &str) -> String {
    print!("{domanda}: ");
    io::stdout().flush().ok();
    let mut riga = String::new();
    io::stdin().lock().read_line(&mut riga).ok();
    riga.trim().to_string()
}

fn valore(variabile: &str, domanda: &str) -> String {
    std::env::var(variabile).unwrap_or_else(|_| chiedi(domanda))
}

/// Le opzioni di `cup cerca`: (zona, giorni da evitare, conferma gia' data).
fn opzioni(argomenti: &[String]) -> Result<(Dove, Calendario, bool), String> {
    let (mut dove, mut val, mut cal, mut si) = ("comune".to_string(), String::new(), Calendario::default(), false);
    let mut it = argomenti.iter();
    while let Some(a) = it.next() {
        let mut segue = || it.next().cloned().ok_or(format!("manca il valore di {a}"));
        match a.as_str() {
            "--dove" => dove = segue()?,
            "--valore" => val = segue()?,
            "--evita" => {
                for g in segue()?.split(',').filter(|g| !g.trim().is_empty()) {
                    cal.no_settimana.push(giorno_settimana(g).ok_or(format!("giorno sconosciuto: {g}"))?);
                }
            }
            "--si" => si = true,
            _ => return Err(format!("opzione sconosciuta: {a}")),
        }
    }
    Ok((Dove::da_scelta(&dove, &val).map_err(|e| e.to_string())?, cal, si))
}

fn attuale(cf: &str, nre: &str) -> Result<(), Errore> {
    let e = Sessione::nuova(cf, nre)?.elenco()?;
    println!("Prenotazione attuale: {}", breve(&e.attuale.quando));
    println!("  {}", e.attuale.luogo);
    println!("  {}", e.attuale.cosa);
    if e.n_prenotate > 1 {
        println!("Righe prenotate: {}", e.n_prenotate);
        for p in &e.prenotate {
            println!("  - {} | {} | {}", breve(&p.quando), p.luogo.sede, p.cosa);
        }
    }
    Ok(())
}

fn cerca(cf: &str, nre: &str, zona: Zona, cal: Calendario, si: bool) -> Result<(), Errore> {
    let mut s = Sessione::nuova(cf, nre)?;
    let e = s.elenco()?;
    println!("Prenotazione attuale: {} | {}", breve(&e.attuale.quando), e.attuale.luogo);
    println!("Cerco {}.", zona.dove.descrizione(Some(&e.attuale)));
    println!("Attenzione: aprire \"Sposta appuntamento\" tiene bloccata la data proposta per circa 40 minuti.");
    println!(
        "Ogni ricerca ne blocca un'altra: tra una ricerca e l'altra della stessa ricetta aspetta almeno 20 minuti."
    );
    if !si && !chiedi("Procedo? [s/N]").eq_ignore_ascii_case("s") {
        println!("Non ho aperto \"Sposta\".");
        return Ok(());
    }
    let alt = s.alternative(zona.estensioni())?;
    for x in &alt.slots {
        let esito = perche_no(x, Some(&e.attuale), &zona, &cal).map_or("✅".to_string(), |p| format!("  ({p})"));
        let proposta = if x.proposta { " [proposta]" } else { "" };
        println!("{} {} | {}{proposta} {esito}", if esito == "✅" { "→" } else { " " }, breve(&x.quando), x.luogo);
    }
    let buone = alt.slots.iter().filter(|x| perche_no(x, Some(&e.attuale), &zona, &cal).is_none()).count();
    println!("Date offerte: {}, che vanno bene: {buone}.", alt.slots.len());
    if alt.area_incompleta {
        println!("\"Estendi area\" non ha risposto in tempo: le date lontane potrebbero mancare.");
    }
    Ok(())
}

fn main() -> ExitCode {
    let argomenti: Vec<String> = std::env::args().skip(1).collect();
    let esito = match argomenti.first().map(String::as_str) {
        Some("attuale") if argomenti.len() == 1 => {
            attuale(&valore("CUP_CF", "Codice fiscale"), &valore("CUP_NRE", "Numero ricetta"))
        }
        Some("cerca") => match opzioni(&argomenti[1..]) {
            Ok((dove, cal, si)) => cerca(
                &valore("CUP_CF", "Codice fiscale"),
                &valore("CUP_NRE", "Numero ricetta"),
                Zona::new(dove),
                cal,
                si,
            ),
            Err(e) => {
                eprintln!("{e}\n{USO}");
                return ExitCode::from(2);
            }
        },
        _ => {
            eprintln!("{USO}");
            return ExitCode::from(2);
        }
    };
    match esito {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("Errore: {e}");
            ExitCode::FAILURE
        }
    }
}
