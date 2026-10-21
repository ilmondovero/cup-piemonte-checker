//! Gli errori del motore, con testi da mostrare all'utente (mai indirizzi o dati personali).

#[derive(Debug, thiserror::Error)]
pub enum Errore {
    /// Il portale non trova prenotazioni per questo codice fiscale + numero ricetta.
    #[error("{0}")]
    NonTrovata(String),
    /// La ricetta c'e' ma nessuna prenotazione e' in stato PRENOTATO (disdetta, gia' erogata...).
    #[error("La prenotazione risulta in stato {}", .stati.join(", "))]
    NonAttiva { stati: Vec<String> },
    /// Codice fiscale o numero ricetta scritti male: non si manda nulla al portale.
    #[error("{0}")]
    DatiNonValidi(String),
    /// Il portale non ha risposto in tempo.
    #[error("il portale non risponde (timeout)")]
    Timeout,
    /// 502/503/504, connessione rifiutata o caduta: si riprova piu' tardi.
    #[error("portale sovraccarico ({0})")]
    Sovraccarico(String),
    /// La stessa prestazione e' prenotata in due appuntamenti: prima se ne disdice uno, poi si cerca.
    #[error("{0}")]
    Doppione(String),
    /// Pagina diversa da quella attesa: sito cambiato, manutenzione, risposta inattesa.
    #[error("{0}")]
    Inattesa(String),
    /// Conferma (o disdetta) inviata, ma l'esito non si e' potuto verificare: va controllato sul portale.
    #[error("{0}")]
    Incerto(String),
}

impl Errore {
    /// Tutte le prenotazioni della ricetta sono state disdette: la ricetta si puo' prenotare di nuovo.
    pub fn solo_disdette(&self) -> bool {
        match self {
            Errore::NonAttiva { stati } => {
                !stati.is_empty()
                    && stati.iter().all(|s| ["DISDETT", "ANNULLAT", "CANCELLAT"].iter().any(|p| s.starts_with(p)))
            }
            _ => false,
        }
    }
}

impl From<reqwest::Error> for Errore {
    fn from(e: reqwest::Error) -> Self {
        // reqwest mette l'indirizzo nel testo dell'errore, con i parametri: si tiene solo il tipo
        if e.is_timeout() {
            return Errore::Timeout;
        }
        if let Some(codice) = e.status() {
            return match codice.as_u16() {
                502..=504 => Errore::Sovraccarico(format!("HTTP {}", codice.as_u16())),
                n => Errore::Inattesa(format!("HTTP {n}, risposta inattesa")),
            };
        }
        if e.is_connect() || e.is_request() || e.is_body() {
            return Errore::Sovraccarico("connessione non riuscita".into());
        }
        Errore::Inattesa("risposta inattesa del portale".into())
    }
}
