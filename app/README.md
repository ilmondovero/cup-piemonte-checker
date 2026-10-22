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
| 4 | Spostamento e disdetta, solo quando lo chiedi tu | ✅ fatto |
| 5 | Installer per i tre sistemi (GitHub Actions) | ✅ fatto |

**Come si usa.** Si aggiunge una ricetta con nome, codice fiscale e numero ricetta, la zona e i giorni da
evitare. L'app legge la prenotazione (solo lettura) e poi, se la ricetta è attiva, la controlla da sola ogni
20 minuti. Quando esce una data che va bene arriva una notifica del sistema con data, ora e luogo, una
volta sola per data. Chiudendo la finestra l'app resta nella barra di sistema e continua; si esce dal menu
dell'icona. Nelle impostazioni si può farla partire con il computer.

**Controlli.** Ogni controllo apre "Sposta appuntamento" per vedere le date, come il bot: la data proposta
dal portale resta bloccata a quella sessione per circa 40 minuti, e non c'è modo di rilasciarla. Per questo
ogni ricetta si controlla al massimo una volta ogni 20 minuti, anche dopo un riavvio o se la si elimina e la
si aggiunge di nuovo, e l'app non si apre due volte. Se la stessa visita risulta prenotata due volte, l'app
non apre "Sposta", mette in pausa e dice quale delle due disdire. Mette in pausa anche se la prenotazione è
disdetta o erogata, o se il portale non la trova per tre controlli di fila. Se segui la stessa ricetta anche
con il bot, usa uno solo dei due.

**Spostare e disdire.** L'app non sposta mai nulla da sola. Su ogni data trovata c'è "Sposta qui", e sulla
scheda "Disdici": entrambi chiedono conferma mostrando data, ora e luogo. Lo spostamento segue il bot passo
per passo:
- riusa la sessione del controllo che ha trovato la data, che tiene bloccata la proposta, se ha meno di
  35 minuti; altrimenti ne apre una nuova;
- prima di confermare rilegge la prenotazione e controlla che la data vada ancora bene (zona, giorni,
  prima della prenotazione);
- controlla che il Riepilogo riporti la stessa data, lo stesso luogo e la stessa prestazione. Se più
  prestazioni sono prenotate insieme, si spostano tutte o nessuna;
- se il Riepilogo ha delle note, fa la presa visione; le note (per esempio la preparazione all'esame)
  restano nell'app;
- dopo la Conferma rilegge l'elenco con sessioni nuove, fino a 3 minuti, per verificare che sia andata.

La disdetta preme "Disdici" sulla riga giusta e "Sì" nel dialogo, poi verifica allo stesso modo.

Prima della Conferma l'app salva sul disco che l'operazione sta partendo; se non ci riesce, non conferma.
Se dopo la Conferma non riesce a verificare l'esito, o se si chiude a metà, lo dice con una notifica. In quel
caso mette la ricetta in pausa e non fa altre operazioni finché non tocchi "Ho controllato" dopo aver
guardato sul sito del CUP. Su una ricetta si fa una cosa alla volta: mai uno spostamento durante un
controllo.

**Zone e giorni.** Le stesse del bot: solo la sede, il comune della prenotazione o un altro, più comuni,
Torino e prima cintura, la provincia o dove propone il CUP. Per comune, comuni e provincia l'app preme
"Estendi area di ricerca", così compaiono anche le aziende sanitarie più lontane. I comuni del Piemonte
sono in `core/data/comuni_piemonte.txt` (ISTAT). Una data va bene se è nella zona, non cade in un giorno
da evitare ed è prima della prenotazione.

## Installarla

Gli installer sono nella pagina [Releases](https://github.com/ilmondovero/cup-piemonte-checker/releases)
del repository, nelle versioni "App desktop app-v…":

- **Windows**: `CUP Piemonte_…_x64-setup.exe` (oppure il `.msi`). Se manca WebView2 lo scarica l'installer.
- **macOS** (Intel e Apple Silicon): `CUP Piemonte_…_universal.dmg`; si trascina l'app in Applicazioni.
- **Linux**: `.deb` (Debian, Ubuntu), `.rpm` (Fedora, openSUSE) oppure `.AppImage` (le altre; va reso
  eseguibile con `chmod +x`).

Gli installer non sono firmati con un certificato, perché costa e richiede un'identità registrata. Per
questo la prima volta il sistema avvisa:

- **Windows** (SmartScreen, "Windows ha protetto il PC"): "Ulteriori informazioni", poi "Esegui comunque".
- **macOS** ("non può essere aperta" o "è danneggiata"): in Impostazioni di Sistema > Privacy e sicurezza,
  "Apri comunque". Se il pulsante non c'è, da Terminale:
  `xattr -dr com.apple.quarantine "/Applications/CUP Piemonte.app"`.

Gli installer li costruisce GitHub Actions a partire dal codice di questo repository
([`.github/workflows/app-installer.yml`](../.github/workflows/app-installer.yml)): prima girano i test sui
tre sistemi, poi un tag `app-vX.Y.Z` crea una release in bozza, che si pubblica a mano dopo averla
provata. L'app non si aggiorna da sola: per una versione nuova si scarica e si installa sopra la vecchia,
e le ricette restano.

## Com'è fatta

- `core/`: il motore in Rust. Fa le richieste HTTP al portale (JSF/ICEfaces, campo per campo come il
  browser) e legge le pagine. Non ha dipendenze dall'interfaccia.
- `cli/`: il comando `cup` per provare il motore da terminale.
- `desktop/`: l'app [Tauri 2](https://tauri.app/). Usa il browser del sistema (WebView2, WebKit), non
  porta con sé Chromium. `archivio.rs` salva le ricette, `controlli.rs` fa i controlli in un thread a parte,
  `operazioni.rs` sposta e disdice (il percorso sul portale è in `core/src/operazioni.rs`).

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
durati, errori del portale, pause, spostamenti e disdette (riusciti, fermati o incerti), notifiche. Le ricette compaiono con il loro numero nell'app ("ricetta 2"):
nel registro non ci sono codice fiscale, numero ricetta, prestazione né date della visita. Si legge dalle
impostazioni (Registro dell'app), dove c'è anche il percorso del file. Il file ruota oltre i 512 KB.

Non c'è telemetria: l'app non manda nulla a nessuno, tranne le richieste al portale CUP.

## Privacy

Codice fiscale e numero ricetta vanno solo al portale CUP. Sul computer le ricette stanno in un file
cifrato (XChaCha20-Poly1305) nella cartella dei dati dell'app, con la chiave nel portachiavi del sistema
(Gestione credenziali di Windows, Portachiavi di macOS, Secret Service su Linux). Se il portachiavi non
c'è, la chiave sta in un file leggibile solo dall'utente, e l'app lo dice nelle impostazioni.
Nell'interfaccia codice fiscale e ricetta compaiono solo in parte (RSS…19X).
