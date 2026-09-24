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
   verifica subito la prenotazione sul portale e mostra prestazione, data, ora e luogo. Chiede se
   segnalare solo date nella stessa sede o in qualsiasi sede proposta dal CUP.
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

Comandi: `/stato`, `/controlla`, `/dati`, `/modifica`, `/sede`, `/auto`, `/pausa`, `/riprendi`, `/cancella`,
`/privacy`. Chi gestisce il bot ha anche `/admin`.

## Limiti da conoscere

- **Conferma verificata dal vivo una volta** (settembre 2026, spostamento automatico riuscito). Dopo la
  conferma il bot ricontrolla comunque con una sessione nuova e, se l'esito non torna, avvisa subito
  l'utente con il numero del call center. Per provare senza rischi c'è `MODALITA_PROVA=1`.
- **Solo appuntamenti già prenotati.** Il bot anticipa una prenotazione esistente. Non cerca il primo
  appuntamento di una ricetta mai prenotata: quel flusso del portale non è ancora mappato.
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
  gli serve solo la connessione in uscita verso `api.telegram.org` e `cup.isan.csi.it`.

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
| `MAX_UTENTI` | 30 | Utenti registrabili al massimo |
| `INTERVALLO_MIN` | 45 | Minuti tra due controlli dello stesso utente (minimo 30) |
| `ADMIN_INTERVALLO_MIN` | come sopra | Intervallo solo per `ADMIN_CHAT_ID` (minimo 5) |
| `DISTANZA_PORTALE_S` | 20 | Secondi minimi tra due sessioni sul portale, fra tutti gli utenti |
| `MODALITA_PROVA` | 0 | 1 = i pulsanti si fermano al Riepilogo senza confermare |

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
- `deploy/`: servizio systemd e script di installazione.
- `Dockerfile`, `docker-compose.yml`: immagine e avvio con Docker.
