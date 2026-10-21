const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const $ = (id) => document.getElementById(id);
const modulo = $("modulo");
const messaggio = $("messaggio");
let modifica = null; // id della ricetta in modifica, null per una nuova
// "Elimina" chiede conferma per qualche secondo; resta valida anche se nel frattempo le schede si ridisegnano
let daEliminare = { id: null, fino: 0 };

// mai innerHTML: i testi arrivano dal portale
function el(tag, classe, testo) {
  const e = document.createElement(tag);
  if (classe) e.className = classe;
  if (testo !== undefined) e.textContent = testo;
  return e;
}

function pulsante(testo, classe, azione) {
  const b = el("button", classe, testo);
  b.type = "button";
  b.addEventListener("click", azione);
  return b;
}

function avvisa(testo, errore = true) {
  messaggio.textContent = testo;
  messaggio.className = errore ? "errore" : "nota";
  messaggio.hidden = !testo;
}

async function esegui(comando, argomenti, ok) {
  try {
    await invoke(comando, argomenti);
    avvisa(ok || "", false);
    return true;
  } catch (e) {
    avvisa(String(e));
    return false;
  }
}

function riepilogo(r) {
  const e = r.esito;
  if (r.in_corso) return el("p", "stato", "🔄 Controllo in corso… può volerci qualche minuto.");
  if (!e || !e.controllo) return el("p", "stato", r.attiva ? "Primo controllo a breve." : "Nessun controllo ancora.");
  if (e.errore) return el("p", "stato errore", `⚠️ Controllo delle ${e.ora}: ${e.errore}`);
  const buone = e.buone.length;
  const offerte = e.date === 1 ? "1 data offerta" : `${e.date} date offerte`;
  return el("p", buone ? "stato buone" : "stato",
    `Controllo delle ${e.ora}: ${offerte}, ` +
    (buone ? (buone === 1 ? "1 va bene." : `${buone} vanno bene.`) : "nessuna prima della prenotazione."));
}

function scheda(r) {
  const s = el("article", "scheda" + (r.attiva ? "" : " ferma"));
  const testa = el("div", "testa");
  testa.append(el("h2", "", r.nome),
    el("span", "pillola " + (r.in_corso ? "corso" : r.attiva ? "attiva" : "pausa"),
      r.in_corso ? "in controllo" : r.attiva ? "attiva" : "in pausa"));
  s.append(testa);
  const a = r.esito && r.esito.attuale;
  if (a) s.append(el("p", "quando", "📅 " + a.quando), el("p", "dove", "📍 " + a.luogo), el("p", "cosa", "🩺 " + a.cosa));
  s.append(el("p", "nota", `🔎 Cerco ${r.zona} · ${r.giorni}`), riepilogo(r));
  if (r.esito && r.esito.buone.length && !r.in_corso) {
    const lista = el("ul", "date");
    for (const d of r.esito.buone) {
      const li = el("li", "buona");
      li.append(el("span", "quando", d.quando + (d.proposta ? " · proposta" : "")), el("span", "dove", d.luogo));
      lista.append(li);
    }
    s.append(lista, el("p", "nota", "Per prenotarla: sito del CUP Piemonte o call center 800 000 500. L'app non sposta nulla."));
  }
  if (r.esito && r.esito.area_incompleta) {
    s.append(el("p", "nota", "\"Estendi area\" non ha risposto in tempo: le date più lontane potrebbero mancare."));
  }
  if (r.attiva && r.prossimo && !r.in_corso) s.append(el("p", "nota", `Prossimo controllo: ${r.prossimo}`));
  s.append(el("p", "nota piccola", `CF ${r.cf} · ricetta ${r.nre}`));

  const azioni = el("div", "azioni");
  azioni.append(pulsante(r.attiva ? "Pausa" : "Riprendi", "secondario",
    () => esegui("attiva", { id: r.id, si: !r.attiva })));
  azioni.append(pulsante("Modifica", "secondario", () => apri(r)));
  const inConferma = daEliminare.id === r.id && Date.now() < daEliminare.fino;
  const elimina = pulsante(inConferma ? "Sicuro? Elimina" : "Elimina", "secondario pericolo", () => {
    if (daEliminare.id === r.id && Date.now() < daEliminare.fino) {
      daEliminare = { id: null, fino: 0 };
      esegui("elimina", { id: r.id });
    } else {
      // due tocchi: il primo chiede conferma per qualche secondo
      daEliminare = { id: r.id, fino: Date.now() + 5000 };
      elimina.textContent = "Sicuro? Elimina";
      setTimeout(carica, 5100);
    }
  });
  azioni.append(elimina);
  s.append(azioni);
  return s;
}

async function carica() {
  try {
    const { ricette, avviso } = await invoke("ricette");
    $("ricette").replaceChildren(...ricette.map(scheda));
    $("vuoto").hidden = ricette.length > 0;
    $("avviso").textContent = avviso || "";
    $("avviso").hidden = !avviso;
    if (!$("impostazioni").hidden && $("dettagli-registro").open) caricaRegistro();
  } catch (e) {
    avvisa(String(e));
  }
}

async function caricaImpostazioni() {
  const i = await invoke("impostazioni");
  $("avvio").checked = i.avvio_automatico;
  $("protezione").textContent = i.protezione === "portachiavi"
    ? "🔒 Le ricette sono salvate cifrate su questo computer, con la chiave nel portachiavi del sistema."
    : "🔒 Le ricette sono salvate cifrate, ma il portachiavi del sistema non c'è: la chiave è in un file accanto " +
      "ai dati, nella cartella del tuo utente. È meno sicuro: chi può leggere i tuoi file può leggere anche le ricette.";
  $("file-registro").textContent = i.registro;
  const parte = $("messo-da-parte");
  parte.hidden = !i.messo_da_parte;
  if (i.messo_da_parte) {
    parte.textContent = "Le ricette salvate prima non si potevano più aprire (chiave cambiata): le ho messe da parte " +
      "in " + i.messo_da_parte + " e ho ricominciato da capo.";
    $("impostazioni").hidden = false;
  }
}

async function caricaRegistro() {
  try {
    const righe = await invoke("registro");
    $("registro").textContent = righe.length ? righe.join("\n") : "Ancora niente.";
  } catch (e) {
    $("registro").textContent = String(e);
  }
}

function sceltaDove(dove, valore) {
  // "comuni" e un "comune" scritto dall'utente si scrivono nella casella; il comune della prenotazione e' "comune" vuoto
  if (dove === "comuni" || (dove === "comune" && valore)) return ["altro", valore];
  return [dove, ""];
}

function apri(r) {
  modifica = r ? r.id : null;
  modulo.reset();
  $("titolo-modulo").textContent = r ? `Modifica: ${r.nome}` : "Nuova ricetta";
  $("credenziali").hidden = !!r; // codice fiscale e ricetta non si cambiano: si aggiunge un'altra ricetta
  $("riga-attiva").hidden = !!r;
  modulo.cf.required = modulo.nre.required = !r;
  if (r) {
    const [dove, valore] = sceltaDove(r.dove, r.valore);
    modulo.nome.value = r.nome;
    modulo.dove.value = dove;
    modulo.valore.value = valore;
    for (const c of modulo.querySelectorAll("input[name=evita]")) c.checked = r.evita.includes(Number(c.value));
  }
  modulo.valore.hidden = modulo.dove.value !== "altro";
  modulo.hidden = false;
  $("nuova").hidden = true;
  modulo.nome.focus();
}

function chiudi() {
  modulo.hidden = true;
  $("nuova").hidden = false;
  modifica = null;
}

modulo.dove.addEventListener("change", () => {
  modulo.valore.hidden = modulo.dove.value !== "altro";
  if (!modulo.valore.hidden) modulo.valore.focus();
});

modulo.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const scelta = modulo.dove.value;
  const valore = modulo.valore.value.trim();
  if (scelta === "altro" && !valore) {
    avvisa("Scrivi il comune in cui cercare.");
    return;
  }
  const comune = { ...(scelta === "altro" ? { dove: valore.includes(",") ? "comuni" : "comune", valore } : { dove: scelta, valore: "" }) };
  const evita = [...modulo.querySelectorAll("input[name=evita]:checked")].map((x) => Number(x.value));
  const salva = $("salva");
  salva.disabled = true;
  if (modifica === null) avvisa("Leggo la prenotazione dal portale… può volerci un minuto.", false);
  const ok = modifica === null
    ? await esegui("aggiungi", { nome: modulo.nome.value, cf: modulo.cf.value, nre: modulo.nre.value, ...comune, evita,
                                 attiva: modulo.attiva.checked }, "Ricetta aggiunta.")
    : await esegui("modifica", { id: modifica, nome: modulo.nome.value, ...comune, evita }, "Modifiche salvate.");
  salva.disabled = false;
  if (ok) chiudi();
});

$("annulla").addEventListener("click", () => { chiudi(); avvisa(""); });
$("nuova").addEventListener("click", () => apri(null));
$("apri-impostazioni").addEventListener("click", (ev) => {
  const p = $("impostazioni");
  p.hidden = !p.hidden;
  ev.currentTarget.setAttribute("aria-expanded", String(!p.hidden));
});
$("dettagli-registro").addEventListener("toggle", (ev) => { if (ev.target.open) caricaRegistro(); });
$("avvio").addEventListener("change", async (ev) => {
  if (!(await esegui("imposta_avvio", { si: ev.target.checked }))) ev.target.checked = !ev.target.checked;
});

listen("ricette", carica);
carica();
caricaImpostazioni();
// gli orari "prossimo controllo" e "oggi/ieri" cambiano col tempo
setInterval(carica, 60000);
