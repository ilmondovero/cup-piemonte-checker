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
   **dove cercare** (vedi "Dove cercare" qui sotto): stessa sede, un comune (quello della prenotazione o
   un altro, per esempio Novara), Torino e prima cintura, la provincia, oppure qualsiasi sede proposta
   dal CUP.
   - Per sedi scelte, comuni e provincia il bot preme "Estendi area di ricerca" del portale, fino a 4 volte: le
     aziende sanitarie lontane compaiono solo così, e solo se hanno posti. Il controllo è più lento
     (anche un minuto e mezzo), ma vede tutta la regione.
   - **Più ricette per chat, senza limite** (`MAX_PRATICHE` lo reintroduce). Con `/aggiungi` si segue
     anche la ricetta di un familiare. Ogni ricetta ha nome, area, conferma automatica e offerte proprie,
     e i messaggi portano il suo nome davanti, per esempio "[Mamma]". Oltre 6 ricette il pannello fissato
     diventa compatto: una riga e un pulsante per ricetta, che apre la sua scheda con tutti i pulsanti.
2. **Controlli periodici** (default ogni 45 minuti per utente). Per ogni utente il bot apre
   *Recupera Prenotazioni → Sposta appuntamento → Altre disponibilità* e legge le date offerte.
3. **Offerta.** Se c'è una data prima di quella attuale, invia data, ora e luogo con il pulsante
   **"✅ Prenota"**, valido 20 minuti. "🔄 Controlla ora" la ripropone con il pulsante anche se era già
   stata offerta.
   - **Tutte le date trovate, anche senza Mini App.** Il pannello ha "📅 N date trovate: vedi e
     prenota" e c'è `/date`: l'elenco dell'ultimo controllo, diviso tra quelle prima della tua
     prenotazione dove cerchi, quelle dopo e quelle in altre zone, ognuna con "Prenota". Prima di
     prenotare il bot chiede conferma con data, ora e luogo, e dice se la data è fuori zona o dopo la
     prenotazione attuale. Valgono 20 minuti dal controllo, poi serve un controllo nuovo.
4. **Prenotazione.** Seleziona lo slot e va al Riepilogo. Conferma solo se il Riepilogo riporta la
   stessa prestazione, la data scelta e lo stesso luogo. Poi verifica con una sessione nuova che la
   prenotazione risulti davvero spostata. Se l'esito è incerto, avvisa subito con il numero del call
   center (800 000 500).
   - **Stato "in corso".** Mentre prenota, la scheda dell'app mostra data, ora e luogo della data nuova
     e la fase: "Sposto a…", "Conferma inviata, aspetto la risposta del portale…", "Conferma inviata,
     verifico sul portale…". Il pannello in chat la mostra dalla verifica (prima della Conferma il bot
     non aspetta Telegram). A fine operazione (riuscita, fallita, incerta o per un errore imprevisto) lo
     stato sparisce; se il processo si ferma dopo la Conferma resta, e al riavvio diventa esito incerto.
     L'app, solo in quel momento, si aggiorna ogni 5 secondi.
   - **Verifica rapida.** Dopo la Conferma (anche se il portale risponde con un errore o non risponde)
     il bot verifica con pause crescenti (2, 5, 10, 20, 40 secondi), in tutto al massimo 3 minuti:
     nessuna richiesta aspetta oltre, e con meno di 30 secondi rimasti non tenta piu'.
   - **Da verificare.** Se la verifica non chiude, la data nuova resta "⚠️ Da verificare" su pannello e
     scheda, sotto la prenotazione vera. La prenotazione salvata cambia solo quando il portale la
     mostra: il bot non decide nulla (date migliori, calendario, automatica) su una data non verificata.
     Il controllo successivo legge il portale e dice com'è andata.
   - **Riavvio a metà.** Se il bot si ferma durante una prenotazione, all'avvio: Conferma già partita →
     esito incerto, automatica spenta e verifica al prossimo controllo (anticipato); Conferma non ancora
     partita → avvisa che la prenotazione non è cambiata.
   - **Giorni guadagnati.** Il bot ricorda la prima prenotazione che ha visto (`prima`: data e sede; per
     una ricetta mai prenotata, la prima prenotazione fatta). Scheda e pannello mostrano quanti giorni
     mancano ("tra 9 giorni") e di quanto è anticipata (o posticipata, col calendario) rispetto alla
     prima; il messaggio dopo uno spostamento lo ripete ("In tutto hai anticipato di N giorni…").
5. **Conferma automatica (facoltativa, `/auto`).** Le date buone spariscono in pochi minuti. Chi la
   attiva lascia che il bot prenoti da solo la prima data migliore, senza aspettare il tocco.
   - È solo accesa o spenta. Rispetta le sedi scelte e il calendario dei giorni sì/no (vedi
     "Calendario" sotto), e non prenota mai per oggi: il primo giorno buono lo decide il calendario.
   - I pulsanti "Da domani / tra 3 / tra 7 giorni" dei messaggi vecchi accendono ancora l'automatica.
     Le ricette salvate con le regole di prima (anticipo minimo, giorni scelti) vengono lette come
     calendario: l'anticipo diventa "no fino al giorno prima", i giorni della settimana scelti lasciano
     no gli altri, le date precise diventano i soli giorni sì. Fascia oraria ed "entro" non ci sono più.
   - Fa un solo tentativo per ogni data.
   - Dopo un esito incerto si disattiva da sola e avvisa l'utente.
   - Prima di attivarla il bot ricorda due cose: la data vecchia si perde, e se poi non si può andare
     bisogna disdire almeno 2 giorni lavorativi prima, altrimenti si paga la prestazione.

Comandi: `/stato`, `/date`, `/controlla`, `/aggiungi`, `/dati`, `/modifica`, `/sede`, `/auto`, `/pausa`, `/riprendi`, `/cancella`,
`/privacy`. Chi gestisce il bot ha anche `/admin`.

## Dove cercare

Ogni ricetta ha la sua zona: una data va bene solo se la sede è lì.

- **Stessa sede** della prenotazione (o una sede vista nei controlli).
- **Un comune**: quello della prenotazione o un altro scritto a mano.
- **Alcuni comuni**: un elenco, fino a 30. Il preset **"Torino e prima cintura"** (in chat e nella Mini
  App) vale per Torino, Beinasco, Borgaro Torinese, Collegno, Grugliasco, Moncalieri, Nichelino,
  Orbassano, Rivoli, San Mauro Torinese, Settimo Torinese e Venaria Reale. Un elenco scelto a mano si fa
  solo dalla Mini App: i comuni entro 25 km da un centro, dal più vicino, con i km in linea d'aria, da
  spuntare. Compaiono solo i comuni dove il bot ha già visto sedi del CUP, più quelli già scelti (anche
  lontani o senza sedi, per poterli togliere).
- **Sedi scelte** (solo dalla Mini App): fino a 20 sedi precise, spuntate tra quelle del registro nei comuni
  entro 25 km dal centro di "Questi comuni" (più quelle già scelte). Ogni sede è la coppia sede + comune,
  perché ci sono sedi omonime in comuni diversi; nel modulo viaggiano come coppie `[sede, comune]` in JSON.
- **La provincia**, oppure **ovunque proponga il CUP**.

Il comune di una sede si legge dall'indirizzo del portale; se l'indirizzo non lo riporta, dal nome della
sede ("PRESIDIO - SUSA"). Se non si legge, la data non viene proposta. I nomi si confrontano senza
accenti e apostrofi ("Mondovì" = "MONDOVI'"). L'elenco dei 1180 comuni del Piemonte, con provincia e
coordinate del centro, è in `comuni_piemonte.txt` (ISTAT). Per sedi scelte, comune, comuni e provincia il bot estende
l'area di ricerca del portale, come detto sopra.

Le sedi viste nei controlli di tutte le ricette finiscono in un registro comune (tabella `sedi`: solo nome
della sede, comune e ora dell'ultima volta che il portale l'ha mostrata, in chiaro perché sono dati
pubblici del portale; niente codice fiscale, NRE o chat). All'avvio il bot lo completa con le sedi già
salvate nelle ricette. Il registro decide quali comuni compaiono in "Questi comuni" e quali sedi in "Sedi scelte".

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
  conferma solo se il Riepilogo riporta le prestazioni del carrello, la data e il luogo scelti. Poi
  ricontrolla con una sessione nuova che la prenotazione risulti davvero fatta.

**Ricette con più prestazioni** (sperimentale anche questo): il bot le cerca e le prenota tutte insieme,
come le propone il portale, senza mai toglierne una. Conferma solo se nel Riepilogo ci sono tutte le
prestazioni e tutte nello stesso appuntamento, quello scelto. Se il portale le mette in date diverse, il
bot non conferma e lo dice: in quel caso si prenota dal portale o al call center. Il numero di prestazioni
deve tornare in ogni passo (se il carrello cambia tra un passo e l'altro, niente Conferma), e ogni nome deve
comparire intero nel Riepilogo, contando anche i doppioni. Dopo la Conferma la prenotazione è riuscita solo
se l'elenco del portale le mostra tutte al posto scelto; altrimenti è un "esito incerto", con l'avviso
all'utente e al gestore.

Per una ricetta mai prenotata con più prestazioni il bot prenota **solo la data proposta dal portale**
(il suo "Avanti", come farebbe una persona): dal vivo, scegliendo una data di "Altre disponibilità" il bot
non è arrivato al Riepilogo, mentre accettando la proposta la ricetta è stata prenotata tutta. Le altre date si vedono ma non si prenotano dal bot. Se la conferma automatica fallisce
su una ricetta così, il bot la spegne per quella ricetta: ogni tentativo terrebbe occupate date per niente.

Quando sposta un appuntamento con più prestazioni prenotate insieme, conferma solo se le sposta tutte. Se il
portale ne sposterebbe una sola, il bot non conferma, mette in pausa i controlli (che terrebbero bloccate
date per niente) e indica portale e call center.

Per capire come il portale presenta questi casi, per ogni passo il bot scrive nel log com'è fatta la
pagina: sezioni, id di form e pulsanti, e numeri (prestazioni nel carrello, caselle spuntate, date,
righe dell'elenco, date per prestazione, cosa risponde il portale a "Seleziona" e "Avanti"). Mai nomi di
prestazioni, date, luoghi o dati della ricetta. Se una prenotazione fallisce, il log ne riporta il motivo
con codice fiscale e numero ricetta mascherati. Lo fa solo per le
prenotazioni nuove e per le ricette con più prestazioni.

Dove cercare: un comune, alcuni comuni (per esempio Torino e prima cintura), oppure dove propone il CUP;
dopo il primo controllo anche una provincia o una sede tra quelle trovate. Ogni data nella zona scelta arriva con il pulsante "✅ Prenota". La conferma
automatica si può attivare anche qui. Dopo la prima prenotazione la ricetta diventa una prenotazione come le
altre e il bot cerca date ancora prima. Se nel frattempo la ricetta viene prenotata a mano, il bot se ne
accorge e passa da solo ad anticipare quella prenotazione.

## Mini App (facoltativa)

Con `WEBAPP_URL` impostato il bot serve anche una **Mini App Telegram**. Si apre dal pulsante "📱 App"
accanto al campo di scrittura o dal pannello in chat. Ogni ricetta ha la sua scheda con prenotazione
attuale, dove cerco, prenotazione automatica, ultimo e prossimo controllo, e quattro pulsanti:
"🔄 Controlla ora", "⏸ Pausa", "📈 Andamento", "✏️ Modifica". Dall'app si può fare tutto quello che si
fa in chat.

Quando il bot trova una data, il messaggio ha anche "📱 Apri nell'app": l'app si apre già su quella
ricetta, con il pulsante **Prenota** di Telegram in basso (la prima data offerta, con la conferma nativa).
Tornando all'app da un'altra chat le schede si ricaricano subito, così non resta sullo schermo una data
vecchia. Cosa si fa dall'app:

- **Ricette.** Aggiungere una ricetta (codice fiscale, NRE, nome; per il primo utente anche il consenso
  all'informativa). Da "✏️ Modifica": rinominarla, sostituirla con un'altra ricetta, cancellarla. Da 🔒
  anche "cancella tutti i miei dati". La ricerca sul portale la fa il bot e l'app aspetta il risultato.
- **Dove cerco** (si tocca la riga sulla scheda). Stessa sede, comune, provincia, ovunque, oppure una
  delle sedi trovate nei controlli. I comuni trovati compaiono come suggerimenti. Con **"Questi comuni"**
  si spuntano i comuni vicini, ordinati per distanza dal centro: il comune della prenotazione, oppure
  Torino per una ricetta mai prenotata; "Centra qui" cambia il centro senza perdere le spunte. Si vedono
  solo i comuni entro 25 km dove il bot ha visto sedi del CUP, con "🏥" e il nome della sede (o quante
  sono), più quelli già spuntati, segnati "nessuna sede vista" se non ne hanno. Finché il bot non ha visto
  sedi vicine l'elenco lo dice. "＋ Torino e prima cintura" spunta il preset e mostra i suoi comuni.
  Con **"Sedi scelte"** si spuntano le singole sedi viste, dalla più vicina allo stesso centro.
- **Prenoto da solo** (si tocca la riga sulla scheda). La conferma automatica: sì o no.
- **Calendario** (si tocca la riga "📅 Calendario" sulla scheda). Un mese alla volta, da quello
  corrente fino a 12 mesi dopo (◀ ▶). Ogni giorno è sì o no: un tocco lo cambia. Un tocco sul nome del
  giorno in alto (lun…dom) rende no (o di nuovo sì) tutti quei giorni. "No fino al giorno…" e poi un
  tocco su un giorno: tutti i giorni fino a quello compreso diventano no. "Tutti sì" azzera. Le modifiche
  restano sul telefono finché non si tocca "Salva" (una richiesta sola). Nel calendario ci sono anche la
  prenotazione attuale (📌) e le date trovate nell'ultimo controllo (puntino); i giorni passati non si
  toccano. Le regole:
  - si propone e si prenota solo una data in un giorno sì, dove si cerca e prenotabile;
  - se la prenotazione è in un giorno sì, solo date prima;
  - se la prenotazione è in un giorno no, va bene anche un giorno sì più tardi, senza limite (anche con
    la conferma automatica). Dopo lo spostamento la prenotazione è in un giorno sì: da lì si anticipa
    soltanto, niente spostamenti a catena;
  - per una ricetta mai prenotata, qualunque giorno sì.
  Al momento di prenotare il bot rilegge il calendario e la prenotazione dal portale: una data più tardi
  passa solo se la prenotazione è davvero in un giorno no. Il CUP mostra solo la prima data libera di ogni
  sede: un giorno preciso si trova se è quella.
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
  giorni, e l'attesa imparata per l'ora corrente. Le metriche stanno nel database (solo orario, durata
  ed esito, nessun dato personale), quindi sopravvivono ai riavvii.
  - **Sorveglianza:** se il ciclo del bot gira ancora e l'ora dell'ultima sessione riuscita sul portale.
  - **Rapporto guasti** (ultime 24 ore o 7 giorni): per ogni passo del portale (elenco, appuntamenti,
    estendi area, riepilogo, conferma, verifica) le richieste, gli errori per tipo (sovraccarico,
    timeout, risposta inattesa) e il tempo tipico e massimo; gli errori per ora del giorno, in grafico e
    in tabella; i tempi delle ultime prenotazioni, dal controllo che ha trovato la data a ogni fase.
    Solo tempi, codici HTTP ed esiti.

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
  e l'errore descrive la pagina (solo nomi di form e pulsanti, nessun dato personale). Anche le ricette
  con più prestazioni sono sperimentali: il bot prenota solo se il portale le mette tutte nello stesso
  appuntamento.
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
- **Portale lento nelle ore di punta.** In orario d'ufficio la ricerca delle date può metterci più di un
  minuto. Il bot non usa una tabella di "ore di punta": impara dai controlli dei 7 giorni precedenti.
  - Per ogni fascia oraria calcola quanto aspettare una risposta: 1,5 volte le risposte più lente, tra 60
    e 180 secondi. Un timeout conta come una risposta lunga almeno quanto l'attesa di allora, così i
    timeout alzano l'attesa invece di sparire dal calcolo. Se a quell'ora i timeout sono frequenti
    (almeno 3 controlli su 10) usa il massimo. Gli altri errori non contano.
  - Dopo un timeout, per quella ricetta aspetta il 50% in più, fino al massimo. Dopo un successo la
    pazienza in più cala piano. Se il portale non risponde proprio (collegamento rifiutato o assente)
    non aspetta di più: per collegarsi bastano 10 secondi.
  - **Un'attesa per ogni passo.** Il bot registra ogni richiesta per passo (elenco, appuntamenti,
    estendi area, ricerca, riepilogo, conferma, verifica) e impara l'attesa di ciascun passo con la
    stessa regola. L'elenco, che risponde in pochi secondi, non aspetta come "Estendi area". Un passo con
    pochi dati in quell'ora usa l'attesa della fascia.
  - Le prenotazioni usano sempre l'attesa massima: sono rare e una data persa costa. Fa eccezione la
    verifica dopo la Conferma, che usa l'attesa imparata e deve stare nei 3 minuti.
  - **Errori con il loro significato.** Nel log ogni errore del portale ha il passo e il codice HTTP (mai
    l'indirizzo, né codice fiscale o NRE). 502, 503, 504, timeout e connessione caduta sono "portale
    sovraccarico": si riprova, e all'utente arriva un messaggio sobrio. Gli altri codici sono "risposta
    inattesa". Per la Conferma ogni errore porta sempre alla verifica e, se non chiude, all'esito incerto.
  - Con errori di fila i controlli si diradano (l'intervallo raddoppia, fino a 60 minuti), per non
    insistere su un portale in difficoltà. Al primo successo si torna al ritmo normale.
  - L'utente viene avvisato al 3°, al 12° e al 40° errore di fila, con l'orario del prossimo controllo,
    e di nuovo quando il portale torna a rispondere.
  - **Sorveglianza.** Chi gestisce il bot riceve un solo avviso quando nessuna sessione sul portale
    riesce da 45 minuti (tutte fallite, o controlli in ritardo di oltre 10 minuti), e uno quando torna
    normale. Se il ciclo del bot non fa un giro e non riceve risposte dal portale da 15 minuti, la Mini
    App (che gira in un altro thread) lo segnala nella pagina Admin e con un avviso, e poi di nuovo
    quando riparte. Stato solo in memoria.

  L'attesa vale per ogni richiesta, e un controllo ne fa diverse (elenco, "Sposta", una per ogni
  estensione dell'area). Mentre aspetta il portale il bot non risponde su Telegram né nella Mini App,
  perché tutto gira in un solo thread: con il portale molto lento, un controllo può tenerlo occupato per
  diversi minuti.
- **IP del server.** Alcuni portali filtrano gli IP dei datacenter. Al momento cup.isan.csi.it non ha
  protezioni anti-bot attive, ma potrebbe cambiare.
- Il sito può cambiare in qualsiasi momento. In quel caso il bot segnala l'errore e non conferma nulla.

## Dati personali

Il bot tratta **dati sanitari di terzi**: codice fiscale, ricetta, prestazione e luogo della visita.
Chi lo mette online ne è responsabile. Il codice fa questo:

- **Cifratura.** Codice fiscale, NRE e ogni informazione sulla prenotazione sono cifrati nel database
  (Fernet, chiave in `CUP_BOT_KEY`). In chiaro restano solo l'id della chat, i campi di pianificazione
  e il registro delle sedi viste (nomi e comuni del portale, senza legame con chat o ricette).
- **Chat pulita.** Cancella dalla chat i messaggi con codice fiscale e NRE appena letti, anche quelli
  scritti fuori dalla registrazione.
- **Consenso prima di tutto.** Finché l'utente non accetta l'informativa, nel database non c'è nulla.
- **Controllo all'utente.** `/dati` mostra i dati mascherati, `/modifica` li cambia, `/cancella` li
  elimina (con `VACUUM` del database).
- **Cancellazione automatica** in quattro casi: la data della prenotazione è passata; la
  registrazione è rimasta incompleta per più di 24 ore; il bot è in pausa da più di 30 giorni;
  l'utente ha bloccato il bot.
- **Log senza dati personali.** I log non contengono codice fiscale, NRE, testo del portale né id
  Telegram (solo un HMAC con chiave). Il diario dei passi del portale contiene solo numeri e id di form
  e pulsanti. Anche il token del bot viene oscurato. Gli avvisi all'admin
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
| `MAX_PRATICHE` | 0 | Ricette seguite al massimo da una stessa chat (0 = nessun limite) |
| `INTERVALLO_MIN` | 45 | Minuti tra due controlli dello stesso utente (minimo 30) |
| `ADMIN_INTERVALLO_MIN` | come sopra | Intervallo solo per `ADMIN_CHAT_ID` (minimo 5) |
| `DISTANZA_PORTALE_S` | 20 | Secondi minimi tra due sessioni sul portale, fra tutti gli utenti (una prenotazione parte subito) |
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
