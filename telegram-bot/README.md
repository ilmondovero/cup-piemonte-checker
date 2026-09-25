# Bot Telegram CUP Piemonte (senza browser)

Bot Telegram **multiutente** che controlla il portale [CUP Piemonte](https://cup.isan.csi.it/) e avvisa
ciascun utente quando si libera una data **prima** della sua prenotazione. Se l'utente tocca il
pulsante nel messaggio, il bot **sposta la prenotazione** su quella data. Senza un tocco non prenota nulla.

A differenza dello script nella cartella principale di questo fork, **non usa un browser**. Parla con il
portale via HTTP e replica campo per campo le richieste JSF/ICEfaces che farebbe il browser. Il
reCAPTCHA del portale è disattivato lato server, quindi non serve aggirare niente. Gira su un server
piccolo, senza Chromium.

## Cosa fa

1. **Registrazione in chat.** Informativa e consenso, poi codice fiscale e numero ricetta (NRE). Il bot
   verifica subito la prenotazione sul portale e mostra prestazione, data, ora e luogo. Se la ricetta non
   è ancora prenotata la segue lo stesso (vedi "Ricetta mai prenotata" qui sotto). Poi chiede
   **dove cercare**: stessa sede, un comune (quello della prenotazione o un altro, per esempio Novara),
   la provincia, oppure qualsiasi sede proposta dal CUP.
   - Per comune e provincia il bot preme "Estendi area di ricerca" del portale, fino a 4 volte: le
     aziende sanitarie lontane compaiono solo così, e solo se hanno posti. Il controllo è più lento
     (anche un minuto e mezzo), ma vede tutta la regione.
   - **Più ricette per chat.** Con `/aggiungi` si segue anche la ricetta di un familiare. Ogni ricetta
     ha nome, area, conferma automatica e offerte proprie, e i messaggi portano il suo nome davanti,
     per esempio "[Mamma]".
2. **Controlli periodici** (default ogni 45 minuti per utente). Per ogni utente il bot apre
   *Recupera Prenotazioni → Sposta appuntamento → Altre disponibilità* e legge le date offerte.
3. **Offerta.** Se c'è una data prima di quella attuale, invia data, ora e luogo con il pulsante
   **"✅ Prenota"**, valido 20 minuti.
4. **Prenotazione.** Seleziona lo slot e va al Riepilogo. Conferma solo se il Riepilogo riporta la
   stessa prestazione, la data scelta e lo stesso luogo. Poi verifica con una sessione nuova che la
   prenotazione risulti davvero spostata. Se l'esito è incerto, avvisa subito con il numero del call
   center (800 000 500).
5. **Conferma automatica (facoltativa, `/auto`).** Le date buone spariscono in pochi minuti. Chi la
   attiva lascia che il bot prenoti da solo la prima data migliore, senza aspettare il tocco.
   - Rispetta le sedi scelte e un anticipo minimo scelto dall'utente: da domani, tra 3 giorni o tra
     7 giorni.
   - Fa un solo tentativo per ogni data.
   - Dopo un esito incerto si disattiva da sola e avvisa l'utente.
   - Prima di attivarla il bot ricorda due cose: la data vecchia si perde, e se poi non si può andare
     bisogna disdire almeno 2 giorni lavorativi prima, altrimenti si paga la prestazione.

Comandi: `/stato`, `/controlla`, `/aggiungi`, `/dati`, `/modifica`, `/sede`, `/auto`, `/pausa`, `/riprendi`, `/cancella`,
`/privacy`. Chi gestisce il bot ha anche `/admin`.

## Ricetta mai prenotata (sperimentale)

Se il portale non trova una prenotazione per codice fiscale + NRE, il bot prova la ricetta come
prenotazione nuova. La procedura del portale ha quattro passi (Ricerca, Prestazioni, Appuntamenti,
Riepilogo e conferma). "Sposta appuntamento" entra direttamente al terzo; qui si parte dal primo:

- **Ricerca** (codice fiscale + NRE, "Prosegui"): alla registrazione il bot fa solo questo passo, che non
  apre gli appuntamenti e non blocca date. Se il portale risponde "già presente", la ricetta ha già un
  appuntamento e si usa il flusso normale.
- **Prestazioni**: il bot preme "Avanti" lasciando le prestazioni come le propone il portale, come farebbe
  una persona. È l'unico passo mai visto dal vivo.
- **Appuntamenti, Riepilogo e Conferma**: gli stessi moduli di "Sposta", con le stesse verifiche. Il bot
  conferma solo se il Riepilogo riporta la prestazione del carrello, la data e il luogo scelti. Poi
  ricontrolla con una sessione nuova che la prenotazione risulti davvero fatta.

Dove cercare: un comune, oppure dove propone il CUP; dopo il primo controllo anche una provincia o una
sede tra quelle trovate. Ogni data nella zona scelta arriva con il pulsante "✅ Prenota". La conferma
automatica si può attivare anche qui. Dopo la prima prenotazione la ricetta diventa una prenotazione come le
altre e il bot cerca date ancora prima. Se nel frattempo la ricetta viene prenotata a mano, il bot se ne
accorge e passa da solo ad anticipare quella prenotazione.

## Mini App (facoltativa)

Con `WEBAPP_URL` impostato il bot serve anche una **Mini App Telegram**. Si apre dal pulsante "📱 App"
accanto al campo di scrittura o dal pannello in chat. Ogni ricetta ha la sua scheda con prenotazione
attuale, dove cerco, prenotazione automatica, ultimo e prossimo controllo, e quattro pulsanti:
"🔄 Controlla ora", "⏸ Pausa", "📈 Andamento", "✏️ Modifica". Dall'app si può fare tutto quello che si
fa in chat:

- **Ricette.** Aggiungere una ricetta (codice fiscale, NRE, nome; per il primo utente anche il consenso
  all'informativa). Da "✏️ Modifica": rinominarla, sostituirla con un'altra ricetta, cancellarla. Da 🔒
  anche "cancella tutti i miei dati". La ricerca sul portale la fa il bot e l'app aspetta il risultato.
- **Dove cerco** (si tocca la riga sulla scheda). Stessa sede, comune, provincia, ovunque, oppure una
  delle sedi trovate nei controlli. I comuni trovati compaiono come suggerimenti.
- **Prenoto da solo** (si tocca la riga sulla scheda). La conferma automatica e l'anticipo minimo.
- **Date disponibili** (dalla riga "📅 3 date disponibili · la prima: …" sulla scheda). Tutte le date
  dell'ultimo controllo, divise in "prima della tua prenotazione, dove cerchi", "dove cerchi, ma dopo la
  tua prenotazione" e "in altre zone". Finché la sessione del controllo è valida (20 minuti) ogni data
  si prenota con un tocco e la conferma nativa di Telegram, anche fuori zona o più tardi dell'attuale:
  è una scelta esplicita dell'utente, e valgono gli stessi controlli sul Riepilogo e dopo la conferma.
  Poi serve un nuovo "🔄 Controlla ora". Un tocco su un comune restringe la ricerca lì.
- **Andamento.** La prima data utile degli ultimi 7 giorni, in grafico e in tabella.
- **Controlla ora e Pausa.** Con le stesse regole della chat: se l'ultimo controllo è di meno di 15
  minuti fa, o c'è un'offerta aperta, l'app lo dice subito. Un'offerta aperta si prenota dalla scheda.
- **Admin** (solo `ADMIN_CHAT_ID`): contatori e tempi delle sessioni sul portale degli ultimi 7
  giorni. Le metriche stanno nel database (solo orario, durata ed esito, nessun dato personale), quindi
  sopravvivono ai riavvii.

Come è protetta:

- **Accesso:** firma `initData` di Telegram, verificata a ogni richiesta con il token del bot
  (intestazione `Authorization: tma …`). Per prenotare, cambiare o cancellare dati serve una firma di
  meno di 2 ore.
- **Permessi:** ognuno vede e modifica solo le sue ricette. Codice fiscale e NRE non compaiono mai.
- **Portale:** le azioni che toccano il portale (cercare una ricetta, controllare, prenotare) passano
  dalla coda del bot. È il bot che tiene le sessioni e le date bloccate, e prenota solo la data esatta
  che l'utente ha confermato, e solo se la prenotazione mostrata nell'app è ancora quella attuale.
- **Limiti:** una ricerca di ricetta alla volta e al massimo 10 al giorno per chat; il bot esegue al
  massimo 20 azioni dell'app per giro; il tetto utenti viene ricontrollato all'attivazione.
- **Sicurezza web:** server in un thread del bot, solo su `127.0.0.1`. Content Security Policy
  rigida, limite di frequenza, nessun cookie. CSS e JS hanno un hash del contenuto nell'indirizzo
  (`?v=…`) e sono serviti come immutabili, così dopo un aggiornamento Telegram non tiene la versione
  vecchia in cache.
- Serve un indirizzo HTTPS pubblico: Telegram apre le Mini App solo così. Esempio con Caddy:

  ```
  app.example.org {
      header Strict-Transport-Security "max-age=31536000"
      reverse_proxy 127.0.0.1:8095 {
          request_buffers 4KiB
      }
  }
  ```

## Limiti da conoscere

- **Conferma.** Il clic su "Conferma" replica il form del Riepilogo come lo invia il browser. Dopo la
  conferma il bot ricontrolla sempre con una sessione nuova e, se l'esito non torna, avvisa subito
  l'utente con il numero del call center. Per provare senza rischi c'è `MODALITA_PROVA=1`.
- **Ricetta mai prenotata: sperimentale.** Il passo "Prestazioni" del portale non è ancora stato visto con
  una ricetta vera (vedi sopra). Se il portale mostra qualcosa di inatteso, il bot si ferma senza prenotare
  e l'errore descrive la pagina (solo nomi di form e pulsanti, nessun dato personale). Ricette con più
  prestazioni: per ora no.
- **Ogni controllo tiene bloccata una data per ~40 minuti.** Quando si apre "Sposta appuntamento", il
  portale riserva la data proposta a quella sessione, e né "Annulla" né il logout la liberano. Succede
  anche a chi lo fa a mano. Per questo:
  - l'intervallo minimo è 30 minuti (default 45) e `/controlla` è possibile al massimo ogni 15 minuti;
  - solo chi gestisce il bot (`ADMIN_CHAT_ID`) può scendere fino a 5 minuti con `ADMIN_INTERVALLO_MIN`.
    Vale per una sola persona, come chi aggiorna la pagina a mano: a 5 minuti tiene bloccate circa 8
    date. Riusare la stessa sessione non aiuta, perché ogni "Sposta" ne blocca una nuova;
  - c'è una distanza minima tra le sessioni sul portale e un tetto al numero di utenti;
  - mentre un'offerta è aperta, quell'utente non viene ricontrollato;
  - la prenotazione continua **nella stessa sessione** che ha trovato la data. Una sessione nuova non
    la vedrebbe finché il blocco non scade.
- **IP del server.** Alcuni portali filtrano gli IP dei datacenter. Al momento cup.isan.csi.it non ha
  protezioni anti-bot attive, ma potrebbe cambiare.
- Il sito può cambiare in qualsiasi momento. In quel caso il bot segnala l'errore e non conferma nulla.

## Dati personali

Il bot tratta **dati sanitari di terzi**: codice fiscale, ricetta, prestazione e luogo della visita.
Chi lo mette online ne è responsabile. Il codice fa questo:

- **Cifratura.** Codice fiscale, NRE e ogni informazione sulla prenotazione sono cifrati nel database
  (Fernet, chiave in `CUP_BOT_KEY`). In chiaro restano solo l'id della chat e i campi di pianificazione.
- **Chat pulita.** Cancella dalla chat i messaggi con codice fiscale e NRE appena letti, anche quelli
  scritti fuori dalla registrazione.
- **Consenso prima di tutto.** Finché l'utente non accetta l'informativa, nel database non c'è nulla.
- **Controllo all'utente.** `/dati` mostra i dati mascherati, `/modifica` li cambia, `/cancella` li
  elimina (con `VACUUM` del database).
- **Cancellazione automatica** in quattro casi: la data della prenotazione è passata; la
  registrazione è rimasta incompleta per più di 24 ore; il bot è in pausa da più di 30 giorni;
  l'utente ha bloccato il bot.
- **Log senza dati personali.** I log non contengono codice fiscale, NRE, testo del portale né id
  Telegram (solo un HMAC con chiave). Anche il token del bot viene oscurato. Gli avvisi all'admin
  riportano solo il tipo di errore.
- **Limiti anti-abuso.**
  - Ogni ricetta può essere seguita da un solo utente (HMAC univoco di codice fiscale + NRE).
  - Al massimo 5 ricerche fallite al giorno per chat.
  - I messaggi a raffica vengono ignorati.
  - Il portale chiede solo codice fiscale e NRE, quindi chiunque li abbia può gestire la prenotazione
    anche dal sito: l'informativa lo dice.
- **Nessun dato a terzi.** I dati vanno solo a cup.isan.csi.it e a Telegram.
- **Solo chat private.** I gruppi vengono ignorati.

Non è un servizio della Regione Piemonte né di CSI Piemonte.

## Installazione su una VPS

Va bene **qualsiasi VPS Linux**, anche la più piccola:
- 1 vCPU e 512 MB di RAM bastano (il bot occupa circa 25 MB);
- niente browser, niente database esterno;
- **nessuna porta in ingresso, dominio o certificato**: il bot usa il long polling di Telegram, quindi
  gli serve solo la connessione in uscita verso `api.telegram.org` e `cup.isan.csi.it`. Fa eccezione
  la Mini App facoltativa, che vuole un indirizzo HTTPS pubblico (vedi sopra).

Prima di tutto crea il bot con [@BotFather](https://t.me/BotFather) (`/newbot`) e copia il token.

### Opzione A: Debian / Ubuntu con systemd

Entra via SSH come root ed esegui:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/ilmondovero/cup-piemonte-checker /opt/cup-piemonte-checker
# leggi lo script prima di eseguirlo come root
bash /opt/cup-piemonte-checker/telegram-bot/deploy/install.sh https://github.com/ilmondovero/cup-piemonte-checker
```

Lo script crea l'utente di sistema `cupbot`, il virtualenv e il servizio systemd (con restrizioni di
sicurezza). Crea anche `/etc/cup-bot.env` (permessi 600) con una **chiave di cifratura nuova**. Poi:

```bash
nano /etc/cup-bot.env            # inserisci TELEGRAM_BOT_TOKEN (e le opzioni che vuoi)
systemctl enable --now cup-bot
journalctl -u cup-bot -f         # log
```

Per aggiornare: `bash /opt/cup-piemonte-checker/telegram-bot/deploy/install.sh <url> [tag-o-commit]`.
Senza tag fa pull dell'ultima versione, con un tag o un commit si ferma su quella. In entrambi i casi
riavvia il servizio. Il database sta in `/var/lib/cup-bot/cup.db`.

### Opzione B: Docker (qualsiasi distribuzione)

Serve Docker con Compose (`docker compose` oppure `docker-compose`).

```bash
git clone https://github.com/ilmondovero/cup-piemonte-checker
cd cup-piemonte-checker/telegram-bot
cp .env.example .env && chmod 600 .env
docker compose build
docker compose run --rm cup-bot python store.py genkey   # copia la chiave in CUP_BOT_KEY dentro .env
nano .env                                                # inserisci TELEGRAM_BOT_TOKEN
docker compose up -d
docker compose logs -f
```

Il container gira come utente senza privilegi, con filesystem in sola lettura e senza capability. Il
database sta nel volume `cup-data`. Per aggiornare: `git pull && docker compose up -d --build`.

### In entrambi i casi

- **Salva una copia di `CUP_BOT_KEY`** in un posto sicuro (per esempio un password manager): senza,
  il database non è più leggibile.
- Se fai un backup del database, tienilo separato dalla chiave. Gli snapshot della VPS copiano anche
  il file di configurazione, quindi la cifratura non protegge gli snapshot.
- Imposta `CONTATTO_GESTORE` (compare nell'informativa agli utenti) e, se vuoi ricevere gli errori,
  `ADMIN_CHAT_ID` con il tuo id Telegram (lo trovi scrivendo a @userinfobot). Deve essere una chat
  privata, non un gruppo.
- Con systemd, limita la durata dei log: `MaxRetentionSec=14day` in `/etc/systemd/journald.conf`, poi
  `systemctl restart systemd-journald`. Con Docker la rotazione dei log è già impostata nel compose.

### Configurazione

| Variabile | Default | Cosa fa |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token di @BotFather (obbligatoria) |
| `CUP_BOT_KEY` | — | Chiave di cifratura del database (obbligatoria) |
| `DB_PATH` | `data/cup.db` | File SQLite |
| `CONTATTO_GESTORE` | — | Contatto del gestore mostrato nell'informativa |
| `ADMIN_CHAT_ID` | — | Chat privata del gestore: riceve gli errori e ha `/admin` |
| `MAX_UTENTI` | 30 | Chat registrabili al massimo |
| `MAX_PRATICHE` | 3 | Ricette seguite al massimo da una stessa chat |
| `INTERVALLO_MIN` | 45 | Minuti tra due controlli dello stesso utente (minimo 30) |
| `ADMIN_INTERVALLO_MIN` | come sopra | Intervallo solo per `ADMIN_CHAT_ID` (minimo 5) |
| `DISTANZA_PORTALE_S` | 20 | Secondi minimi tra due sessioni sul portale, fra tutti gli utenti |
| `MODALITA_PROVA` | 0 | 1 = i pulsanti si fermano al Riepilogo senza confermare |
| `WEBAPP_URL` | — | Indirizzo HTTPS pubblico della Mini App; vuoto = Mini App spenta |
| `WEBAPP_PORTA` | 8095 | Porta locale (`127.0.0.1`) a cui il reverse proxy inoltra la Mini App |

## Sviluppo

```bash
pip install -r requirements.txt pytest
pytest tests          # nessuna richiesta di rete: HTML sintetico, Telegram e portale finti
```

Per provare in locale imposta `TELEGRAM_BOT_TOKEN`, `CUP_BOT_KEY` e `MODALITA_PROVA=1` (i pulsanti si
fermano al Riepilogo), poi `python bot.py`.

File:
- `cup_http.py`: client HTTP del portale (sessione, parsing, prenotazione).
- `store.py`: archivio SQLite cifrato.
- `bot.py`: bot Telegram.
- `webapp.py`, `web/static/`: Mini App (htmx 4.0.0 incluso, CSS e JS senza build).
- `deploy/`: servizio systemd e script di installazione.
- `Dockerfile`, `docker-compose.yml`: immagine e avvio con Docker.
