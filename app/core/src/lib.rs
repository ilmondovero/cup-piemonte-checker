//! Motore dell'app CUP Piemonte: parla con il portale cup.isan.csi.it via HTTP, replicando i POST del
//! browser campo per campo (il portale e' un'applicazione JSF/ICEfaces).
//!
//! Per ora solo lettura: la prenotazione attuale di una ricetta (Recupera Prenotazioni). Non apre
//! "Sposta appuntamento", quindi non blocca date: aprire "Sposta" fa tenere lo slot proposto a quella
//! sessione per ~40 minuti, e non c'e' modo di rilasciarlo.

pub mod analisi;
pub mod errore;
pub mod portale;

pub use analisi::{Elenco, Luogo, Prenotazione};
pub use errore::Errore;
pub use portale::Sessione;
