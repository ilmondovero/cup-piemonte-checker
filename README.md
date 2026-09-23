# CUP Piemonte - controllo disponibilità automatico

![Il checker ce la fa (al posto tuo)](banner.png)

Sei stanco di doverti sedere lì ogni ora, aggiornare la pagina e sperare che
per una volta compaia un appuntamento libero invece del solito "Nessun
appuntamento disponibile"? Bene, da oggi ci pensa il checker al posto tuo.

Visto che il Servizio Sanitario Nazionale non è (ancora?) in grado di
automatizzare in modo decente qualcosa di così banale come avvisarti quando
si libera un posto, ci ha pensato uno stanco cittadino con Python e un bot
Telegram. Nessuna sovvenzione statale, nessuna gara d'appalto, nessun
consulente esterno: solo uno script che controlla al posto tuo e ti scrive
quando c'è qualcosa da prenotare.

## Cos'è

È un programma Python che gira sul tuo PC. Ogni tot minuti apre un vero browser
(invisibile, in background), va su [cup.isan.csi.it](https://cup.isan.csi.it/), inserisce
il tuo codice fiscale e il numero della ricetta esattamente come faresti tu a mano, e
legge la pagina "Appuntamenti": se non trova più la scritta "Nessun appuntamento
disponibile", ti manda un messaggio Telegram.

Il browser reale (non semplici richieste HTTP) serve perché il form di CUP Piemonte è
un'applicazione web "vecchio stile" (JSF/ICEfaces) con sessione e un controllo
anti-bot invisibile: replicare esattamente i click di un utente è il modo più
affidabile perché funzioni.

**Il tuo codice fiscale e il numero ricetta restano solo sul tuo PC**, dentro
`config.json` (mai condiviso con nessuno, creato/aggiornato automaticamente quando premi
Avvia nella interfaccia).

C'è un'interfaccia grafica (`gui.py`, consigliata) e la versione a riga di comando
(`checker.py`, per chi preferisce Task Scheduler).

**Niente gira sul cloud, niente passa da server terzi**: tutto (browser, controllo,
dati) resta sul tuo computer. Questo repository su GitHub contiene solo il codice:
zero codici fiscali, zero token, zero dati personali di nessuno.

## Cosa ti serve prima di iniziare

- **Windows, macOS o Linux** con accesso a internet.
- **Python 3.10 o superiore** installato.
  - Non ce l'hai? Scaricalo da [python.org/downloads](https://www.python.org/downloads/)
    e installalo. **Su Windows, durante l'installazione spunta la casella "Add
    python.exe to PATH"** (è il passaggio che quasi tutti saltano e poi i comandi
    sotto non funzionano) — poi riavvia il terminale.
  - Per controllare se ce l'hai già, apri un terminale (PowerShell su Windows,
    Terminale su macOS/Linux) e scrivi:
    ```bash
    python --version
    ```
    Se vedi un numero tipo `Python 3.11.x` sei a posto.
- **Un account Telegram** (l'app che probabilmente hai già sul telefono), serve solo
  per ricevere la notifica.
- Il tuo **codice fiscale** e il **numero della ricetta elettronica (NRE)** che vuoi
  monitorare (li trovi sul promemoria/ricetta dematerializzata).

Non serve installare Git: basta scaricare lo ZIP del progetto.

## 0. Scarica il progetto

In alto in questa pagina GitHub premi il pulsante verde **"Code"** → **"Download ZIP"**,
poi estrai la cartella `cup-piemonte-checker` dove preferisci (es. Desktop).

Apri un terminale dentro quella cartella (su Windows: apri la cartella in Esplora
File, poi scrivi `cmd` nella barra dell'indirizzo e premi Invio) e da lì copia-incolla
i comandi dei passaggi successivi.

## 1. Installazione (una tantum)

Copia e incolla questi comandi nel terminale, uno alla volta:

```bash
pip install -r requirements.txt
playwright install chromium
```

Il primo installa le librerie Python necessarie, il secondo scarica il browser
(Chromium) che lo script userà per navigare al posto tuo: sono solo file sul tuo PC,
non vengono condivisi con nessuno.

## 2. Crea il bot Telegram (una tantum, ~2 minuti)

Telegram non permette di creare bot in automatico: va fatto una volta a mano via chat,
ma è semplice:

1. Su Telegram cerca **@BotFather**, scrivi `/newbot` e segui le istruzioni: alla fine
   ti dà un **token** tipo `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Copialo.
2. Cerca il bot appena creato (il nome che gli hai dato) e premi **Avvia** (o mandagli
   un messaggio qualsiasi) — è un passaggio obbligato lato Telegram: un bot non può
   scriverti se prima non gli scrivi tu.

Il resto (trovare il tuo "chat ID") lo fa l'app per te: vedi punto 4.

## 3. Avvia l'interfaccia grafica

```bash
python gui.py
```

Si apre una finestra con:

- **Codice Fiscale** e **Numero ricetta elettronica (NRE)**
- **Bot Token** (incollalo da BotFather): appena esci dal campo (o premi Invio) l'app
  verifica da sola se il token è valido e mostra sotto un indicatore colorato:
  - 🟢 **"● Bot collegato: @nomebot"** → token corretto, il bot esiste
  - 🔴 **"● Bot non valido (token errato)"** oppure **"non raggiungibile"** → controlla
    di aver copiato il token giusto o la connessione
- **Chat ID**, con un pulsante **"Rileva Chat ID automaticamente"**: lo trova da solo
  leggendo i messaggi ricevuti dal bot (basta che tu gli abbia scritto/premuto Avvia al
  punto 2). In alternativa c'è **"Invia test"** per verificare subito che arrivi un
  messaggio.
- **Controlla ogni ... minuti** (default 15, minimo 5 per non sovraccaricare il portale)
- Un pulsante grande **Avvia ricerca / Ferma ricerca**
- Un'area di stato e un log dei controlli

Premendo **Avvia ricerca** i dati vengono salvati in `config.json` e parte il
controllo periodico in background (browser invisibile). Quando trova un appuntamento
disponibile ricevi il messaggio su Telegram con uno screenshot.

## 4. Ridurre a icona / riaprire

Chiudendo la finestra (X) il programma **non si chiude**: si nasconde e resta attivo
nella system tray di Windows (le "icone nascoste" vicino all'orologio, cerca l'icona
blu con la "C"). Da lì puoi:

- **click/doppio click** sull'icona → riapre la finestra
- **tasto destro → Avvia/Ferma ricerca** → senza riaprire la finestra
- **tasto destro → Esci** → chiude davvero il programma e ferma i controlli

## Cosa fa quando trova/non trova disponibilità

- **Trova un appuntamento**: manda un messaggio Telegram con screenshot (al giro dopo
  ricontrolla comunque, così sai se lo slot è ancora lì).
- **Nessun appuntamento**: solo aggiornamento nel log della finestra, nessuna notifica.
- **Ricetta non trovata/scaduta/già usata**: ti avvisa su Telegram e **ferma il
  controllo** (continuare non avrebbe senso finché non correggi i dati).
- **Il sito non risponde o ha cambiato struttura**: te lo segnala e continua a
  riprovare da solo ai giri successivi.

## Se qualcosa non torna al primo utilizzo

Con una ricetta reale, se lo script non arriva alla pagina "Appuntamenti" (stato
"Pagina inattesa" nel log), controlla la cartella `screenshots/`: salva un'immagine di
cosa ha visto il browser in quel momento. Mandami lo screenshot o descrivimi cosa
mostra e sistemo i selettori — non ho potuto testare questo passaggio con dati reali
per motivi di privacy (il codice fiscale è un dato d'identità che non gestisco io).

## Uso da riga di comando (alternativa/avanzata)

```bash
cp config.example.json config.json   # compila i campi con un editor di testo
python checker.py --test-telegram    # verifica Telegram
python checker.py --once             # un solo controllo (con headless:false nel config per vedere il browser)
python checker.py                    # loop continuo
```

Utile se vuoi usare **Utilità di pianificazione di Windows (Task Scheduler)** invece
di lasciare la GUI aperta: programma `pythonw.exe`, argomento `checker.py`, directory
di lavoro questa cartella, trigger "All'accesso".
