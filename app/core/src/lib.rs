//! Motore dell'app CUP Piemonte: parla con il portale cup.isan.csi.it via HTTP, replicando i POST del
//! browser campo per campo (il portale e' un'applicazione JSF/ICEfaces).
//!
//! Per ora non prenota ne' sposta: legge la prenotazione attuale (Recupera Prenotazioni) e le date che
//! "Sposta appuntamento" offre, filtrate per zona e giorni da evitare. Attenzione: aprire "Sposta" fa tenere
//! lo slot proposto a quella sessione per ~40 minuti, e non c'e' modo di rilasciarlo.

pub mod analisi;
pub mod errore;
pub mod portale;
pub mod zona;

#[cfg(test)]
mod prova;

pub use analisi::{Elenco, Luogo, Prenotazione, Slot};
pub use errore::Errore;
pub use portale::{Alternative, Controllo, Sessione};
pub use zona::{Calendario, Dove, Sede, Zona};
