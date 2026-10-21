const { invoke } = window.__TAURI__.core;
const form = document.getElementById("ricetta");
const esito = document.getElementById("esito");
const conferma = document.getElementById("conferma");

// mai innerHTML: i testi arrivano dal portale
function el(tag, classe, testo) {
  const e = document.createElement(tag);
  if (classe) e.className = classe;
  if (testo !== undefined) e.textContent = testo;
  return e;
}

function scheda(v) {
  const s = el("div", "scheda");
  s.append(el("p", "quando", "📅 " + v.quando), el("p", "dove", "📍 " + v.luogo), el("p", "cosa", "🩺 " + v.cosa));
  return s;
}

function occupato(si) {
  for (const b of form.querySelectorAll("button")) b.disabled = si;
}

async function chiedi(comando, argomenti, attesa) {
  occupato(true);
  esito.replaceChildren(el("p", "attesa", attesa));
  try {
    return await invoke(comando, argomenti);
  } catch (e) {
    esito.replaceChildren(el("p", "errore", String(e)));
    return null;
  } finally {
    occupato(false);
  }
}

function ricetta() {
  return { cf: form.cf.value, nre: form.nre.value };
}

async function leggi() {
  const r = await chiedi("attuale", ricetta(), "Chiedo al portale… può volerci un minuto.");
  if (!r) return;
  esito.replaceChildren(scheda(r.attuale));
  if (r.prenotate.length > 1) {
    esito.append(el("p", "nota", `La ricetta ha ${r.prenotate.length} righe prenotate:`));
    for (const p of r.prenotate) esito.append(el("p", "altra", `${p.quando} · ${p.sede} · ${p.cosa}`));
  }
}

async function cerca() {
  const scelta = form.dove.value;
  const argomenti = {
    ...ricetta(),
    dove: scelta === "altro" ? (form.valore.value.includes(",") ? "comuni" : "comune") : scelta,
    valore: scelta === "altro" ? form.valore.value : "",
    evita: [...form.querySelectorAll("input[name=evita]:checked")].map((x) => Number(x.value)),
  };
  if (scelta === "altro" && !form.valore.value.trim()) {
    esito.replaceChildren(el("p", "errore", "Scrivi il comune in cui cercare."));
    return;
  }
  const r = await chiedi("cerca", argomenti, "Cerco le date sul portale… con un'area ampia può volerci qualche minuto.");
  if (!r) return;
  esito.replaceChildren(scheda(r.attuale));
  const titolo = r.buone
    ? `${r.buone === 1 ? "1 data va bene" : r.buone + " date vanno bene"} (cerco ${r.zona})`
    : `Nessuna data prima della prenotazione (cerco ${r.zona})`;
  esito.append(el("h2", r.buone ? "buone" : "", titolo));
  const lista = el("ul", "date");
  for (const d of r.date) {
    const riga = el("li", d.motivo ? "scartata" : "buona");
    riga.append(el("span", "quando", d.quando + (d.proposta ? " · proposta" : "")), el("span", "dove", d.luogo));
    if (d.motivo) riga.append(el("span", "motivo", d.motivo));
    lista.append(riga);
  }
  esito.append(lista);
  if (!r.date.length) esito.append(el("p", "nota", "Il portale non ha offerto date."));
  if (r.area_incompleta) {
    esito.append(el("p", "nota", "\"Estendi area\" non ha risposto in tempo: le date più lontane potrebbero mancare."));
  }
}

form.dove.addEventListener("change", () => {
  form.valore.hidden = form.dove.value !== "altro";
  if (!form.valore.hidden) form.valore.focus();
});

form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (ev.submitter && ev.submitter.value === "cerca") {
    conferma.hidden = false; // prima di aprire "Sposta" si chiede: blocca una data
    document.getElementById("procedi").focus();
  } else {
    conferma.hidden = true;
    leggi();
  }
});

document.getElementById("annulla").addEventListener("click", () => {
  conferma.hidden = true;
});

document.getElementById("procedi").addEventListener("click", () => {
  conferma.hidden = true;
  if (form.reportValidity()) cerca();
});
