// in release niente finestra del terminale su Windows
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Instant;

use cup_core::analisi::{breve, credenziali};
use cup_core::portale::INTERVALLO_MIN;
use cup_core::zona::perche_no;
use cup_core::{Calendario, Dove, Errore, Prenotazione, Sessione, Slot, Zona};
use serde::Serialize;

#[derive(Serialize)]
struct Visita {
    quando: String,
    sede: String,
    luogo: String,
    cosa: String,
}

impl From<&Prenotazione> for Visita {
    fn from(p: &Prenotazione) -> Self {
        Visita {
            quando: breve(&p.quando),
            sede: p.luogo.sede.clone(),
            luogo: p.luogo.to_string(),
            cosa: p.cosa.clone(),
        }
    }
}

#[derive(Serialize)]
struct Lettura {
    attuale: Visita,
    prenotate: Vec<Visita>,
}

#[derive(Serialize)]
struct Data {
    quando: String,
    luogo: String,
    proposta: bool,
    /// Perche' la data non va bene; assente se va bene.
    motivo: Option<&'static str>,
}

#[derive(Serialize)]
struct Ricerca {
    attuale: Visita,
    zona: String,
    date: Vec<Data>,
    buone: usize,
    area_incompleta: bool,
}

/// Quando e' partita l'ultima ricerca di ogni ricetta (per numero ricetta), finche' l'app resta aperta.
#[derive(Default)]
struct Ricerche(Mutex<HashMap<String, Instant>>);

/// Il motore usa un client HTTP bloccante: fuori dal thread dell'interfaccia.
async fn in_disparte<T: Send + 'static>(f: impl FnOnce() -> Result<T, Errore> + Send + 'static) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(f).await.map_err(|_| "errore interno".to_string())?.map_err(|e| e.to_string())
}

/// La prenotazione attuale della ricetta, letta dal portale (solo lettura: non blocca date).
#[tauri::command]
async fn attuale(cf: String, nre: String) -> Result<Lettura, String> {
    in_disparte(move || {
        let e = Sessione::nuova(&cf, &nre)?.elenco()?;
        Ok(Lettura { attuale: (&e.attuale).into(), prenotate: e.prenotate.iter().map(Visita::from).collect() })
    })
    .await
}

/// Le date offerte da "Sposta appuntamento" e quelle che vanno bene. Blocca la data proposta per ~40 minuti.
#[tauri::command]
async fn cerca(
    ricerche: tauri::State<'_, Ricerche>,
    cf: String,
    nre: String,
    dove: String,
    valore: String,
    evita: Vec<u32>,
) -> Result<Ricerca, String> {
    let zona = Zona::new(Dove::da_scelta(&dove, &valore).map_err(|e| e.to_string())?);
    let (_, ricetta) = credenziali(&cf, &nre).map_err(|e| e.to_string())?;
    {
        // ogni "Sposta" blocca un'altra data per ~40 minuti: non piu' di una ricerca ogni 20 minuti per ricetta,
        // anche se quella prima e' finita con un errore (puo' aver gia' aperto "Sposta")
        let mut fatte = ricerche.0.lock().map_err(|_| "errore interno".to_string())?;
        if let Some(prima) = fatte.get(&ricetta).filter(|t| t.elapsed() < INTERVALLO_MIN) {
            let resta = (INTERVALLO_MIN - prima.elapsed()).as_secs().div_ceil(60);
            return Err(format!(
                "Hai già cercato le date di questa ricetta da poco: ogni ricerca blocca un'altra data per circa 40                  minuti. Riprova tra {resta} minut{}.",
                if resta == 1 { "o" } else { "i" }
            ));
        }
        fatte.insert(ricetta, Instant::now());
    }
    in_disparte(move || {
        let cal = Calendario { no_settimana: evita.into_iter().filter(|g| *g < 7).collect(), ..Default::default() };
        let c = Sessione::nuova(&cf, &nre)?.controlla(&zona, &cal)?;
        let att = &c.elenco.attuale;
        let data = |s: &Slot| Data {
            quando: breve(&s.quando),
            luogo: s.luogo.to_string(),
            proposta: s.proposta,
            motivo: perche_no(s, Some(att), &zona, &cal),
        };
        Ok(Ricerca {
            attuale: att.into(),
            zona: zona.dove.descrizione(Some(att)),
            date: c.slots.iter().map(data).collect(),
            buone: c.migliori.len(),
            area_incompleta: c.area_incompleta,
        })
    })
    .await
}

fn main() {
    tauri::Builder::default()
        .manage(Ricerche::default())
        .invoke_handler(tauri::generate_handler![attuale, cerca])
        .run(tauri::generate_context!())
        .expect("avvio dell'app non riuscito");
}
