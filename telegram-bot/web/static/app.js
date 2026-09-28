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
  // pulsante "Prenota" di Telegram, in basso sotto il pollice: la prima data della ricetta aperta dal
  // messaggio del bot (?r=<id>), altrimenti la prima offerta in pagina. Con un foglio aperto si nasconde
  const principale = tg && tg.MainButton;
  const richiesta = new URLSearchParams(location.search).get("r");
  let portato = false; // la scheda della ricetta richiesta si porta in vista una volta sola
  const formPrincipale = () => {
    const sua = /^\d+$/.test(richiesta || "") && document.querySelector(`#r-${richiesta} .offerta form[data-conferma]`);
    return sua || document.querySelector(".offerta form[data-conferma]");
  };
  const aggiornaPrincipale = () => {
    if (!principale) return;
    const f = foglio.hidden ? formPrincipale() : null;
    const quando = f && f.closest("li") && f.closest("li").querySelector(".quando");
    if (!quando) {
      principale.hide();
      return;
    }
    principale.setText("Prenota " + quando.textContent.trim());
    principale.show();
  };
  if (principale) principale.onClick(() => {
    const f = formPrincipale();
    if (f) f.requestSubmit(); // passa dalla conferma nativa, come il pulsante nella scheda
  });
  new MutationObserver(() => {
    aggiornaPrincipale();
    const scheda = !portato && /^\d+$/.test(richiesta || "") && document.getElementById("r-" + richiesta);
    if (scheda) {
      portato = true;
      scheda.scrollIntoView({ block: "start" });
    }
  }).observe(ricette, { childList: true, subtree: true });

  // tornando all'app (da un'altra chat, o dal messaggio del bot) le schede si ricaricano subito:
  // una data vecchia non deve restare sullo schermo fino al prossimo aggiornamento
  const ricarica = () => ricette.dispatchEvent(new Event("aggiorna"));
  if (tg && tg.onEvent) tg.onEvent("activated", ricarica);
  // una prenotazione in corso (data-in-corso sulla scheda): le schede si aggiornano ogni 5 s finche' c'e',
  // poi di nuovo solo ogni 15 s
  setInterval(() => {
    if (document.visibilityState === "visible" && ricette.querySelector("[data-in-corso]")) ricarica();
  }, 5000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") ricarica();
  });

  const apri = () => {
    foglio.hidden = false;
    velo.hidden = false;
    if (tg) tg.BackButton.show();
    aggiornaPrincipale();
  };
  const chiudi = () => {
    foglio.hidden = true;
    velo.hidden = true;
    foglio.innerHTML = "";
    if (tg) tg.BackButton.hide();
    aggiornaPrincipale();
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

  // --- calendario dei giorni sì/no: lo stato sta nei campi nascosti del form, ogni tocco lo cambia qui
  // (niente richiesta per tocco: il server accetta un'azione ogni 1,5 s) e "Salva" lo manda tutto insieme.
  // Stessa regola di cup_http.giorno_si: no se entro "no fino al", dopo "si_fino", giorno della settimana no
  // o giorno segnato no.
  const piuGiorni = (iso, n) => {
    const t = new Date(iso + "T00:00:00Z");
    t.setUTCDate(t.getUTCDate() + n);
    return t.toISOString().slice(0, 10);
  };
  const statoCal = (f) => {
    const lista = (nome) => f.elements[nome].value.split(",").filter(Boolean);
    return { no: new Set(lista("no")), sett: new Set(lista("no_settimana")),
             fino: f.elements.no_fino.value, siFino: f.elements.si_fino.value };
  };
  const siFisso = (s, d, w) => !(s.fino && d <= s.fino) && !(s.siFino && d > s.siFino) && !s.sett.has(w);
  const scriviCal = (f, s) => {
    f.elements.no.value = [...s.no].sort().join(",");
    f.elements.no_settimana.value = [...s.sett].sort().join(",");
    f.elements.no_fino.value = s.fino;
    f.elements.si_fino.value = s.siFino;
    for (const b of f.querySelectorAll(".cal-sett")) {
      const no = s.sett.has(b.dataset.w);
      b.classList.toggle("no", no);
      b.setAttribute("aria-pressed", String(no));
    }
    for (const b of f.querySelectorAll("button.g")) {
      const fisso = siFisso(s, b.dataset.d, b.dataset.w);
      const si = fisso && !s.no.has(b.dataset.d);
      b.classList.toggle("si", si);
      b.classList.toggle("no", !si);
      b.classList.toggle("fisso", !fisso);
      b.setAttribute("aria-pressed", String(!si));
      b.setAttribute("aria-label", b.getAttribute("aria-label").replace(/: (sì|no)/, si ? ": sì" : ": no"));
    }
  };
  const aiutoCal = (f, testo) => {
    const p = f.querySelector(".cal-aiuto");
    if (!p.dataset.base) p.dataset.base = p.textContent;
    p.textContent = testo || p.dataset.base;
  };
  const modoFino = (f, acceso) => {
    f.classList.toggle("modo-fino", acceso);
    f.querySelector(".cal-fino").setAttribute("aria-pressed", String(acceso));
    aiutoCal(f, acceso ? "Tocca l’ultimo giorno no: tutti i giorni fino a quello compreso diventano no." : "");
  };
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest(".calendario button");
    if (!b || b.type === "submit") return;
    const f = b.closest("form");
    const s = statoCal(f);
    const oggi = f.dataset.oggi;
    if (b.classList.contains("cal-prec") || b.classList.contains("cal-succ")) {
      const mese = b.closest(".cal-mese");
      const altro = b.classList.contains("cal-prec") ? mese.previousElementSibling : mese.nextElementSibling;
      if (altro && altro.classList.contains("cal-mese")) {
        mese.hidden = true;
        altro.hidden = false;
      }
      return;
    }
    if (b.classList.contains("cal-fino")) {
      modoFino(f, !f.classList.contains("modo-fino"));
      return;
    }
    if (b.classList.contains("cal-tutti")) {
      modoFino(f, false);
      scriviCal(f, { no: new Set(), sett: new Set(), fino: "", siFino: "" });
      aiutoCal(f, "Tutti i giorni sono sì: premi Salva per tenerli così.");
      return;
    }
    if (b.classList.contains("cal-sett")) {
      const w = b.dataset.w;
      if (s.sett.has(w)) s.sett.delete(w);
      else s.sett.add(w);
      aiutoCal(f, "");
      scriviCal(f, s);
      return;
    }
    const d = b.dataset.d;
    if (!d) return;
    aiutoCal(f, "");
    if (f.classList.contains("modo-fino")) {
      s.fino = d;
      modoFino(f, false);
    } else if (s.fino && d <= s.fino) {
      // dentro "no fino al": il giorno toccato torna sì, e con lui quelli dopo
      s.fino = piuGiorni(d, -1) < oggi ? "" : piuGiorni(d, -1);
    } else if (s.siFino && d > s.siFino) {
      // dopo l'ultimo giorno sì: il periodo si allunga fino a qui, i giorni in mezzo restano no
      for (let x = piuGiorni(s.siFino, 1); x < d; x = piuGiorni(x, 1)) s.no.add(x);
      s.siFino = d;
      s.no.delete(d);
    } else if (s.sett.has(b.dataset.w)) {
      const nome = f.querySelector(`.cal-sett[data-w="${b.dataset.w}"]`).textContent;
      aiutoCal(f, `Tutti i ${nome} sono no: tocca «${nome}» in alto per cambiarli.`);
      return;
    } else if (s.no.has(d)) s.no.delete(d);
    else s.no.add(d);
    scriviCal(f, s);
  });
  document.addEventListener("submit", (e) => {
    // prima dell'invio si tolgono i giorni passati e quelli gia' no per altre regole: il server li
    // toglierebbe comunque, ma contano per il limite di date
    const f = e.target;
    if (!f.classList || !f.classList.contains("calendario")) return;
    const s = statoCal(f);
    for (const d of [...s.no]) {
      const w = String((new Date(d + "T00:00:00Z").getUTCDay() + 6) % 7);
      if (d < f.dataset.oggi || !siFisso(s, d, w)) s.no.delete(d);
    }
    scriviCal(f, s);
  }, true);

  // --- "Questi comuni" e "Sedi scelte" nel foglio "Dove cerco": le spunte stanno nel campo nascosto
  // "comuni" (separati da virgola) o "sedi" (coppie [sede, comune] in JSON: nei nomi ci sono virgole),
  // anche quelle dei comuni nascosti o fuori elenco, che il server legge al Salva e a "Centra qui". Le
  // righe data-preset (comuni della cintura senza sedi viste) compaiono solo con "Torino e prima cintura"
  const spunte = (box, cambia) => {
    const campo = box.querySelector('input[type="hidden"]');
    const sedi = campo.name === "sedi";
    const s = new Set(sedi ? JSON.parse(campo.value || "[]").map((x) => JSON.stringify(x))
                           : campo.value.split(",").filter(Boolean));
    cambia(s);
    campo.value = sedi ? `[${[...s].join(",")}]` : [...s].join(",");
    const scelta = box.closest("form").querySelector(`input[name="tipo"][value="${campo.name}"]`);
    if (scelta) scelta.checked = true;
  };
  document.addEventListener("change", (e) => {
    const c = e.target;
    if (!c.matches || !c.matches('.comuni-scelta .cm input[type="checkbox"]')) return;
    const box = c.closest(".comuni-scelta");
    const v = box.querySelector('input[name="sedi"]') ? JSON.stringify(JSON.parse(c.value)) : c.value;
    spunte(box, (s) => (c.checked ? s.add(v) : s.delete(v)));
  });
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest(".cm-preset, .cm-altri");
    if (!b) return;
    const box = b.closest(".comuni-scelta");
    if (b.classList.contains("cm-altri")) {
      for (const r of box.querySelectorAll(".cm[hidden]:not([data-preset])")) r.hidden = false;
      b.hidden = true;
      return;
    }
    const preset = b.dataset.comuni.split(",");
    spunte(box, (s) => preset.forEach((n) => s.add(n)));
    for (const c of box.querySelectorAll(".cm input")) {
      if (!preset.includes(c.value)) continue;
      c.checked = true;
      c.closest(".cm").hidden = false;
    }
  });
  document.addEventListener("keydown", (e) => {
    // Invio nel campo del centro ricarica l'elenco ("Centra qui") invece di salvare il foglio
    const t = e.target;
    if (e.key !== "Enter" || !t.matches || !t.matches(".cm-centro input")) return;
    e.preventDefault();
    t.closest(".cm-centro").querySelector("button").click();
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
    const invia = () => {
      if (principale && principale.isVisible) principale.showProgress();
      return fetch(url, {
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
      }, () => avvisa("Non sono riuscito a inviare la richiesta: controlla la connessione."))
        .finally(() => {
          if (principale) principale.hideProgress();
          ricarica();
        });
    };
    if (tg && tg.showConfirm) tg.showConfirm(f.dataset.conferma, (ok) => ok && invia());
    else if (window.confirm(f.dataset.conferma)) invia();
  }, true);
})();
