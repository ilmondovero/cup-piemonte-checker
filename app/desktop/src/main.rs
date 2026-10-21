// in release niente finestra del terminale su Windows
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use cup_core::analisi::breve;
use cup_core::{Prenotazione, Sessione};
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
struct Esito {
    attuale: Visita,
    prenotate: Vec<Visita>,
}

/// La prenotazione attuale della ricetta, letta dal portale (solo lettura: non blocca date).
#[tauri::command]
async fn attuale(cf: String, nre: String) -> Result<Esito, String> {
    // il client HTTP e' bloccante: fuori dal thread dell'interfaccia
    tauri::async_runtime::spawn_blocking(move || {
        let e = Sessione::nuova(&cf, &nre)?.elenco()?;
        Ok(Esito { attuale: (&e.attuale).into(), prenotate: e.prenotate.iter().map(Visita::from).collect() })
    })
    .await
    .map_err(|_| "errore interno".to_string())?
    .map_err(|e: cup_core::Errore| e.to_string())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![attuale])
        .run(tauri::generate_context!())
        .expect("avvio dell'app non riuscito");
}
