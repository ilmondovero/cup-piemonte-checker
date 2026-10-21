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
| 3 | Ricette salvate e cifrate, controlli ogni 20 minuti, notifiche, icona nella barra di sistema | ✅ fatto |
| 4 | Spostamento e disdetta | da fare |
| 5 | Installer per i tre sistemi (GitHub Actions) | da fare |

**Come si usa.** Si aggiunge una ricetta con nome, codice fiscale e numero ricetta, la zona e i giorni da
evitare. L'app legge la prenotazione (solo lettura) e poi, se la ricetta è attiva, la controlla da sola ogni
20 minuti. Quando esce una data che va bene arriva una notifica del sistema con data, ora e luogo, una
volta sola per data. Chiudendo la finestra l'app resta nella barra di sistema e continua; si esce dal menu
dell'icona. Nelle impostazioni si può farla partire con il computer.

**Non sposta nulla.** Ogni controllo apre "Sposta appuntamento" per vedere le date, come il bot: la data
proposta dal portale resta bloccata a quella sessione per circa 40 minuti, e non c'è modo di rilasciarla.
Per questo ogni ricetta si controlla al massimo una volta ogni 20 minuti, anche dopo un riavvio o se la si
elimina e la si aggiunge di nuovo, e l'app non si apre due volte. Se la stessa visita risulta prenotata due volte, l'app non apre "Sposta", mette in pausa
e dice quale delle due disdire. Mette in pausa anche se la prenotazione è disdetta o erogata, o se il
portale non la trova per tre controlli di fila. Se segui la stessa ricetta anche con il bot, usa uno solo
dei due.

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
  porta con sé Chromium. `archivio.rs` salva le ricette, `controlli.rs` fa i controlli in un thread a parte.

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

## Registro

L'app scrive in un file di testo cosa fa e quando: avvio, controlli con quante date ha trovato e quanto sono
durati, errori del portale, pause, notifiche. Le ricette compaiono con il loro numero nell'app ("ricetta 2"):
nel registro non ci sono codice fiscale, numero ricetta, prestazione né date della visita. Si legge dalle
impostazioni (Registro dell'app), dove c'è anche il percorso del file. Il file ruota oltre i 512 KB.

Non c'è telemetria: l'app non manda nulla a nessuno, tranne le richieste al portale CUP.

## Privacy

Codice fiscale e numero ricetta vanno solo al portale CUP. Sul computer le ricette stanno in un file
cifrato (XChaCha20-Poly1305) nella cartella dei dati dell'app, con la chiave nel portachiavi del sistema
(Gestione credenziali di Windows, Portachiavi di macOS, Secret Service su Linux). Se il portachiavi non
c'è, la chiave sta in un file leggibile solo dall'utente, e l'app lo dice nelle impostazioni.
Nell'interfaccia codice fiscale e ricetta compaiono solo in parte (RSS…19X).
