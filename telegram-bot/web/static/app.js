// Mini App: firma Telegram su ogni richiesta, foglio dal basso per le impostazioni, conferme native.
(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg ? tg.initData : "";
  const ricette = document.getElementById("ricette");
  const foglio = document.getElementById("foglio");
  const velo = document.getElementById("velo");
  let invioDalFoglio = false;

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
      invioDalFoglio = true;
    }
  }, true);
  document.addEventListener("htmx:after:request", () => {
    if (invioDalFoglio) {
      invioDalFoglio = false;
      chiudi();
      vibra();
    }
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
    const avvisa = (t) => (tg && tg.showAlert ? tg.showAlert(t) : window.alert(t));
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
          // DOMParser legge il testo senza eseguire nulla della risposta
          const testo = new DOMParser().parseFromString(await r.text(), "text/html").body.textContent.trim();
          avvisa(testo || "Non sono riuscito a inviare la richiesta.");
        }
        ricette.dispatchEvent(new Event("aggiorna"));
      });
    if (tg && tg.showConfirm) tg.showConfirm(f.dataset.conferma, (ok) => ok && invia());
    else if (window.confirm(f.dataset.conferma)) invia();
  }, true);
})();
