//! Motore dell'app CUP Piemonte: parla con il portale cup.isan.csi.it via HTTP, replicando i POST del
//! browser campo per campo (il portale e' un'applicazione JSF/ICEfaces).
//!
//! Legge la prenotazione attuale (Recupera Prenotazioni) e le date che "Sposta appuntamento" offre, filtrate
//! per zona e giorni da evitare; sposta la prenotazione su una di quelle date o la disdice (vedi `operazioni`),
//! solo quando lo chiede l'utente. Attenzione: aprire "Sposta" fa tenere lo slot proposto a quella sessione per
//! ~40 minuti, e non c'e' modo di rilasciarlo.

pub mod analisi;
pub mod errore;
pub mod operazioni;
pub mod portale;
pub mod zona;

#[cfg(test)]
mod prova;

pub use analisi::{Elenco, Luogo, Prenotazione, Slot};
pub use errore::Errore;
pub use portale::{Alternative, Controllo, Sessione};
pub use zona::{Calendario, Dove, Sede, Zona};
