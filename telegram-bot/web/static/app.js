// Mini App: firma Telegram su ogni richiesta, foglio dal basso per le impostazioni, conferme native.
(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg ? tg.initData : "";
  const ricette = document.getElementById("ricette");
  const foglio = document.getElementById("foglio");
  const velo = document.getElementById("velo");
  let formDelFoglio = null; // form del foglio appena inviato: il foglio si chiude alla SUA risposta

  if (tg) {
    tg.ready();
    tg.expand();
  }
  if (!initData) {
    ricette.innerHTML = '<p class="errore">Apri l’app dal pulsante del bot su Telegram.</p>';
  }

  // ogni richiesta porta la firma di Telegram (il server la verifica sempre), nell'intestazione
  // standard delle Mini App: i proxy non la scrivono nei log
  const firma = "tma " + initData;
  document.addEventListener("htmx:config:request", (e) => {
    // htmx 4: le intestazioni sono in detail.ctx.request.headers (in htmx 2 erano detail.headers)
    const headers = e.detail.ctx ? e.detail.ctx.request.headers : e.detail.headers;
    headers["Authorization"] = firma;
  });

  const vibra = () => tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("success");
  const avvisa = (t) => (tg && tg.showAlert ? tg.showAlert(t) : window.alert(t));
  // DOMParser legge il testo senza eseguire nulla della risposta
  const testoDi = (html) => new DOMParser().parseFromString(html || "", "text/html").body.textContent.trim();

  // l'errore di un'azione (dati non validi, "una cosa alla volta"...) non deve prendere il posto delle
  // schede: htmx 4 lo inserirebbe nella pagina, invece diventa un avviso di Telegram. L'errore di una
  // lettura (GET) resta dov'e': nel foglio o al posto delle schede spiega cosa fare, e ferma un'attesa.
  document.addEventListener("htmx:response:error", (e) => {
    const ctx = e.detail.ctx;
    if (!ctx || (ctx.request && String(ctx.request.method).toUpperCase() === "GET")) return;
    ctx.swap = "none";
    avvisa(testoDi(ctx.text) || "Non sono riuscito a inviare la richiesta.");
  });
  document.addEventListener("htmx:error", (e) => {
    // rete assente: nessuna risposta da mostrare. Solo per le azioni, non per gli aggiornamenti automatici
    // (ogni 15 s le schede, ogni 2 s un'attesa), che da offline farebbero un avviso dopo l'altro
    const ctx = e.detail && e.detail.ctx;
    const azione = ctx && ctx.request && String(ctx.request.method).toUpperCase() !== "GET";
    if (azione && !navigator.onLine) avvisa("Sei offline: riprova quando torna la connessione.");
  });
  const apri = () => {
    foglio.hidden = false;
    velo.hidden = false;
    if (tg) tg.BackButton.show();
  };
  const chiudi = () => {
    foglio.hidden = true;
    velo.hidden = true;
    foglio.innerHTML = "";
    if (tg) tg.BackButton.hide();
  };
  if (tg) tg.BackButton.onClick(chiudi);
  velo.addEventListener("click", chiudi);

  // il foglio si apre quando htmx ci mette dentro un contenuto
  new MutationObserver(() => {
    if (foglio.innerHTML.trim() && foglio.hidden) apri();
  }).observe(foglio, { childList: true });

  // dopo "Salva" nel foglio: si chiude quando la risposta e' arrivata
  document.addEventListener("submit", (e) => {
    // i form "data-resta" (ricerca di una ricetta) proseguono nel foglio invece di chiuderlo
    // i form con conferma partono con fetch (niente htmx:after:request): si chiudono da soli dopo l'invio
    const f = e.target;
    if (f.closest && f.closest("#foglio") && !f.hasAttribute("data-resta") && !(f.dataset && f.dataset.conferma)) {
      formDelFoglio = f;
    }
  }, true);
  document.addEventListener("htmx:after:request", (e) => {
    // le risposte degli aggiornamenti automatici non contano: solo quella del form inviato
    const ctx = e.detail && e.detail.ctx;
    if (!formDelFoglio || !ctx || ctx.sourceElement !== formDelFoglio) return;
    formDelFoglio = null;
    if (ctx.response && ctx.response.status >= 400) return; // errore: il foglio resta aperto per correggere
    chiudi();
    vibra();
  });
  document.addEventListener("htmx:error", (e) => {
    // invio fallito per la rete: nessuna risposta arrivera', il foglio resta aperto
    const ctx = e.detail && e.detail.ctx;
    if (ctx && ctx.sourceElement === formDelFoglio) formDelFoglio = null;
  });

  // prenotazione: conferma nativa di Telegram, poi invio e aggiornamento delle schede
  document.addEventListener("submit", (e) => {
    const f = e.target;
    if (!f.dataset || !f.dataset.conferma) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    // i dati si fissano PRIMA della conferma: mentre la finestra e' aperta le schede possono aggiornarsi
    const url = f.action;
    const corpo = new URLSearchParams(new FormData(f));
    const invia = () =>
      fetch(url, {
        method: "POST",
        headers: { Authorization: firma, "Content-Type": "application/x-www-form-urlencoded" },
        body: corpo,
      }).then(async (r) => {
        if (r.ok) {
          vibra();
          chiudi();
        } else {
          avvisa(testoDi(await r.text()) || "Non sono riuscito a inviare la richiesta.");
        }
        ricette.dispatchEvent(new Event("aggiorna"));
      });
    if (tg && tg.showConfirm) tg.showConfirm(f.dataset.conferma, (ok) => ok && invia());
    else if (window.confirm(f.dataset.conferma)) invia();
  }, true);
})();
