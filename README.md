# CUP Piemonte - controllo disponibilità automatico

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

## 1. Installazione (una tantum)

```bash
cd cup-piemonte-checker
pip install -r requirements.txt
playwright install chromium
```

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
