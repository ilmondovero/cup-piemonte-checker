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

Comandi: `/stato`, `/controlla`, `/dati`, `/modifica`, `/sede`, `/pausa`, `/riprendi`, `/cancella`,
`/privacy`. Chi gestisce il bot ha anche `/admin`.

## Limiti da conoscere

- **Solo appuntamenti già prenotati.** Il bot anticipa una prenotazione esistente. Non cerca il primo
  appuntamento di una ricetta mai prenotata: quel flusso del portale non è ancora mappato.
- **Ogni controllo tiene bloccata una data per ~40 minuti.** Quando si apre "Sposta appuntamento", il
  portale riserva la data proposta a quella sessione, e né "Annulla" né il logout la liberano. Succede
  anche a chi lo fa a mano. Per questo:
  - l'intervallo minimo è 30 minuti (default 45) e `/controlla` è possibile al massimo ogni 15 minuti;
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

## Installazione su un server Debian/Ubuntu

1. Crea il bot con [@BotFather](https://t.me/BotFather) (`/newbot`) e copia il token.
2. Su un server Debian/Ubuntu (basta un piccolo VPS), entra via SSH come root ed esegui:

   ```bash
   apt-get update && apt-get install -y git
   git clone https://github.com/ilmondovero/cup-piemonte-checker /opt/cup-piemonte-checker
   # leggi lo script prima di eseguirlo come root
   bash /opt/cup-piemonte-checker/telegram-bot/deploy/install.sh https://github.com/ilmondovero/cup-piemonte-checker
   ```

   Lo script crea l'utente di sistema `cupbot`, il virtualenv e il servizio systemd. Crea anche
   `/etc/cup-bot.env` (permessi 600) con una **chiave di cifratura nuova**.
3. **Salva una copia di `CUP_BOT_KEY`** in un posto sicuro: senza, il database non è più leggibile.
   Poi inserisci il token e, se vuoi, il tuo chat id come `ADMIN_CHAT_ID`:

   ```bash
   nano /etc/cup-bot.env
   systemctl enable --now cup-bot
   journalctl -u cup-bot -f
   ```

4. Aggiornamenti: `bash /opt/cup-piemonte-checker/telegram-bot/deploy/install.sh <url> [tag-o-commit]`.
   Senza tag fa pull dell'ultima versione, con un tag o un commit si ferma su quella. In entrambi i casi
   riavvia il servizio.

Il database sta in `/var/lib/cup-bot/cup.db`. Se fai un backup, tienilo separato dalla chiave. Gli
snapshot del server copiano anche `/etc/cup-bot.env`, quindi la cifratura non protegge gli snapshot.

Consigliato: limita la durata dei log con `MaxRetentionSec=14day` in `/etc/systemd/journald.conf`,
poi `systemctl restart systemd-journald`. Imposta anche `CONTATTO_GESTORE` in `/etc/cup-bot.env`:
l'informativa lo mostra agli utenti. `ADMIN_CHAT_ID` deve essere una chat privata, non un gruppo.

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
