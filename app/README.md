# App desktop CUP Piemonte (in sviluppo)

Un'app leggera per Windows, macOS e Linux che controlla il portale [CUP Piemonte](https://cup.isan.csi.it/)
dal tuo computer. È un progetto separato dal bot Telegram della cartella [`../telegram-bot/`](../telegram-bot/):
non usa Telegram né un server e non condivide codice con il bot. Del bot riprende le regole sul
comportamento del portale, che lì sono già state verificate.

## A che punto è

| Passo | Cosa | Stato |
|---|---|---|
| 1 | Motore: legge la prenotazione attuale (codice fiscale + numero ricetta) | ✅ fatto |
| 2 | Ricerca delle date alternative (zona, giorni da evitare), senza spostare nulla | ✅ fatto |
| 3 | Interfaccia completa, icona nella barra di sistema, notifiche native | da fare |
| 4 | Spostamento e disdetta | da fare |
| 5 | Installer per i tre sistemi (GitHub Actions) | da fare |

**Non sposta nulla.** "Leggi la prenotazione" è solo lettura. "Cerca date prima" apre "Sposta appuntamento"
per vedere le date, come il bot a ogni controllo: la data proposta dal portale resta bloccata a quella
sessione per circa 40 minuti, e non c'è modo di rilasciarla. Per questo l'app chiede conferma ogni volta.
Ogni ricerca ne blocca un'altra, quindi per la stessa ricetta l'app ne fa al massimo una ogni 20 minuti,
come il bot. Se la stessa visita risulta prenotata due volte, l'app non apre "Sposta" e dice quale delle
due disdire.

**Zone e giorni.** Le stesse del bot: solo la sede, il comune della prenotazione o un altro, più comuni,
Torino e prima cintura, la provincia o dove propone il CUP. Per comune, comuni e provincia l'app preme
"Estendi area di ricerca", così compaiono anche le aziende sanitarie più lontane. I comuni del Piemonte
sono in `core/data/comuni_piemonte.txt` (ISTAT). Una data va bene se è nella zona, non cade in un giorno
da evitare ed è prima della prenotazione.

## Com'è fatta

- `core/`: il motore in Rust. Fa le richieste HTTP al portale (JSF/ICEfaces, campo per campo come il
  browser) e legge le pagine. Non ha dipendenze dall'interfaccia.
- `cli/`: il comando `cup` per provare il motore da terminale.
- `desktop/`: l'app [Tauri 2](https://tauri.app/). Usa il browser del sistema (WebView2, WebKit), non
  porta con sé Chromium.

## Provarla

Serve [Rust](https://rustup.rs/). Su Linux servono anche le librerie di WebKitGTK
(vedi i [prerequisiti di Tauri](https://tauri.app/start/prerequisites/)).

```sh
cd app
cargo test -p cup-core                      # test del motore, senza rete
cargo run -p cup-cli -- attuale             # chiede codice fiscale e numero ricetta
cargo run -p cup-cli -- cerca --dove cintura --evita ven   # date prima, chiede conferma
cargo run -p cup-desktop                    # apre la finestra
```

Il comando `cup` legge codice fiscale e numero ricetta da `CUP_CF` e `CUP_NRE` oppure li chiede. Non li
accetta come argomenti, perché resterebbero nella cronologia dei comandi.

## Privacy

Codice fiscale e numero ricetta vanno solo al portale CUP. L'app non li manda altrove, e per ora non li
salva: vanno scritti a ogni lettura. Quando servirà salvarli, staranno cifrati sul computer, con la
chiave nel portachiavi del sistema.
