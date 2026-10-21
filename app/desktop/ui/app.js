const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const $ = (id) => document.getElementById(id);
const modulo = $("modulo");
const messaggio = $("messaggio");
let modifica = null; // id della ricetta in modifica, null per una nuova
// "Elimina" chiede conferma per qualche secondo; resta valida anche se nel frattempo le schede si ridisegnano
let daEliminare = { id: null, fino: 0 };
// lo spostamento o la disdetta da confermare: { id, tipo: "sposta" | "disdici", data } (data: per "sposta")
let daConfermare = null;
// le operazioni partite da questa finestra, finche' il portale non risponde (contro il doppio click)
const inViaggio = new Set();
const IN_CORSO = {
  controllo: "🔄 Controllo in corso… può volerci qualche minuto.",
  spostamento: "🔄 Spostamento in corso: dopo la conferma l'app controlla sul portale che sia andato. Può volerci " +
    "qualche minuto, non spegnere il computer.",
  disdetta: "🔄 Disdetta in corso: dopo la conferma l'app controlla sul portale. Non spegnere il computer.",
};
const PILLOLA = { controllo: "in controllo", spostamento: "spostamento", disdetta: "disdetta" };

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
  if (r.in_corso) return el("p", "stato", IN_CORSO[r.in_corso]);
  if (e && e.nota) return el("p", r.incerta ? "stato" : "stato buone", e.nota);
  if (!e || !e.controllo) return el("p", "stato", r.attiva ? "Primo controllo a breve." : "Nessun controllo ancora.");
  if (e.errore) return el("p", "stato errore", `⚠️ Controllo delle ${e.ora}: ${e.errore}`);
  const buone = e.buone.length;
  const offerte = e.date === 1 ? "1 data offerta" : `${e.date} date offerte`;
  return el("p", buone ? "stato buone" : "stato",
    `Controllo delle ${e.ora}: ${offerte}, ` +
    (buone ? (buone === 1 ? "1 va bene." : `${buone} vanno bene.`) : "nessuna prima della prenotazione."));
}

async function operazione(comando, argomenti, id) {
  if (inViaggio.has(id)) return; // doppio clic: l'operazione e' gia' partita
  daConfermare = null;
  inViaggio.add(id);
  avvisa(comando === "sposta" ? "Sposto la prenotazione… può volerci qualche minuto." :
    "Disdico la prenotazione… può volerci qualche minuto.", false);
  carica();
  try {
    avvisa(await invoke(comando, argomenti), false);
  } catch (e) {
    avvisa(String(e));
  } finally {
    inViaggio.delete(id);
    carica();
  }
}

// il riquadro "Sei sicuro?" di uno spostamento o di una disdetta: data, ora e luogo, prima di toccare il portale
function riquadroConferma(r, a) {
  const c = daConfermare;
  const box = el("div", "conferma");
  if (c.tipo === "sposta") {
    box.append(el("p", "titolo", "Spostare la prenotazione?"),
      el("p", "", "Da: " + (a ? `${a.quando} · ${a.luogo}` : "la prenotazione attuale")),
      el("p", "quando", `A: ${c.data.quando} · ${c.data.luogo}`),
      el("p", "nota", "L'app conferma sul portale e poi controlla che lo spostamento sia andato. La data attuale " +
        "viene liberata."));
  } else {
    box.append(el("p", "titolo", "Disdire la prenotazione?"),
      el("p", "quando", `${a.quando} · ${a.luogo}`),
      el("p", "nota", "Non si può annullare: per riaverla va prenotata di nuovo, e la data potrebbe non esserci più."));
  }
  const bottoni = el("div", "pulsanti");
  bottoni.append(pulsante("Annulla", "secondario", () => { daConfermare = null; carica(); }),
    c.tipo === "sposta"
      ? pulsante("Sì, sposta", "", () => operazione("sposta", { id: r.id, chiave: c.data.chiave }, r.id))
      : pulsante("Sì, disdici", "pericolo-pieno", () => operazione("disdici", { id: r.id }, r.id)));
  box.append(bottoni);
  return box;
}

function scheda(r) {
  const s = el("article", "scheda" + (r.attiva ? "" : " ferma"));
  const testa = el("div", "testa");
  testa.append(el("h2", "", r.nome),
    el("span", "pillola " + (r.in_corso ? "corso" : r.attiva ? "attiva" : "pausa"),
      r.in_corso ? PILLOLA[r.in_corso] : r.attiva ? "attiva" : "in pausa"));
  s.append(testa);
  if (r.incerta) {
    const box = el("div", "conferma incerta");
    box.append(el("p", "errore", "⚠️ " + r.incerta),
      pulsante("Ho controllato", "secondario", () => esegui("chiudi_incerta", { id: r.id })));
    s.append(box);
  }
  const a = r.esito && r.esito.attuale;
  if (a) s.append(el("p", "quando", "📅 " + a.quando), el("p", "dove", "📍 " + a.luogo), el("p", "cosa", "🩺 " + a.cosa));
  s.append(el("p", "nota", `🔎 Cerco ${r.zona} · ${r.giorni}`), riepilogo(r));
  const libera = !r.in_corso && !r.incerta && !inViaggio.has(r.id);
  if (r.esito && r.esito.note_visita.length) {
    const note = el("details", "note");
    note.append(el("summary", "", "Note per la visita"));
    for (const n of r.esito.note_visita) note.append(el("p", "nota", n));
    s.append(note);
  }
  if (r.esito && r.esito.buone.length && !r.in_corso) {
    const lista = el("ul", "date");
    for (const d of r.esito.buone) {
      const li = el("li", "buona");
      li.append(el("span", "quando", d.quando + (d.proposta ? " · proposta" : "")), el("span", "dove", d.luogo));
      if (libera) {
        li.append(pulsante("Sposta qui", "secondario", () => {
          daConfermare = { id: r.id, tipo: "sposta", data: d };
          carica();
        }));
      }
      lista.append(li);
    }
    s.append(lista, el("p", "nota", "Puoi spostarla qui con \"Sposta qui\", o dal sito del CUP Piemonte o al call " +
      "center 800 000 500. L'app non sposta mai nulla da sola."));
  }
  // il riquadro solo finche' la data e' ancora tra quelle trovate (un controllo puo' averle cambiate)
  const c = daConfermare;
  const valida = c && c.id === r.id && (c.tipo === "disdici"
    ? a && a.iso : r.esito && r.esito.buone.some((d) => d.chiave === c.data.chiave));
  if (valida && libera) s.append(riquadroConferma(r, a));
  if (r.esito && r.esito.area_incompleta) {
    s.append(el("p", "nota", "\"Estendi area\" non ha risposto in tempo: le date più lontane potrebbero mancare."));
  }
  if (r.attiva && r.prossimo && !r.in_corso) s.append(el("p", "nota", `Prossimo controllo: ${r.prossimo}`));
  s.append(el("p", "nota piccola", `CF ${r.cf} · ricetta ${r.nre}`));

  const azioni = el("div", "azioni");
  azioni.append(pulsante(r.attiva ? "Pausa" : "Riprendi", "secondario",
    () => esegui("attiva", { id: r.id, si: !r.attiva })));
  azioni.append(pulsante("Modifica", "secondario", () => apri(r)));
  // serve la data letta da un controllo (le prenotazioni salvate prima del passo 4 non ce l'hanno)
  if (a && a.iso && libera) {
    azioni.append(pulsante("Disdici", "secondario pericolo", () => {
      daConfermare = { id: r.id, tipo: "disdici" };
      carica();
    }));
  }
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
  // durante uno spostamento o una disdetta la ricetta non si elimina (l'app lo rifiuterebbe)
  if (r.in_corso !== "spostamento" && r.in_corso !== "disdetta") azioni.append(elimina);
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
