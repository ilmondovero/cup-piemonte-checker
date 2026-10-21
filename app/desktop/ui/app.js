const { invoke } = window.__TAURI__.core;
const form = document.getElementById("ricetta");
const esito = document.getElementById("esito");

function riga(classe, testo) {
  const el = document.createElement("p");
  el.className = classe;
  el.textContent = testo; // mai innerHTML: i testi arrivano dal portale
  return el;
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const pulsante = form.querySelector("button");
  pulsante.disabled = true;
  esito.replaceChildren(riga("attesa", "Chiedo al portale… può volerci un minuto."));
  try {
    const r = await invoke("attuale", { cf: form.cf.value, nre: form.nre.value });
    const scheda = document.createElement("div");
    scheda.className = "scheda";
    scheda.append(riga("quando", "📅 " + r.attuale.quando), riga("dove", "📍 " + r.attuale.luogo),
                  riga("cosa", "🩺 " + r.attuale.cosa));
    esito.replaceChildren(scheda);
    if (r.prenotate.length > 1) {
      esito.append(riga("nota", `La ricetta ha ${r.prenotate.length} righe prenotate:`));
      for (const p of r.prenotate) esito.append(riga("altra", `${p.quando} · ${p.sede} · ${p.cosa}`));
    }
  } catch (e) {
    esito.replaceChildren(riga("errore", String(e)));
  } finally {
    pulsante.disabled = false;
  }
});
