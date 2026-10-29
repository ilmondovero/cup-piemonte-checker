// in release niente finestra del terminale su Windows
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod aggiornamenti;
mod archivio;
mod controlli;
mod operazioni;
mod registro;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use chrono::{Local, TimeZone};
use cup_core::analisi::credenziali;
use cup_core::operazioni::Attese;
use cup_core::{Dove, Errore, Prenotazione, Sessione};
use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_notification::NotificationExt;

use aggiornamenti::{Aggiornamenti, Avvio};
use archivio::{Archivio, Data, Esito, Protezione, Ricetta, Visita};
use controlli::{adesso, zona_di, Attivita, Notifica, Stato, INTERVALLO};
use operazioni::Fine;
use registro::Registro;

type Condiviso<'a> = State<'a, Arc<Stato>>;
type Canale<'a> = State<'a, Arc<Aggiornamenti>>;

/// "14:05" oggi, "21/10 14:05" un altro giorno.
fn ora(ts: i64) -> String {
    let t = Local.timestamp_opt(ts, 0).single().unwrap_or_else(Local::now);
    if t.date_naive() == Local::now().date_naive() {
        t.format("%H:%M").to_string()
    } else {
        t.format("%d/%m %H:%M").to_string()
    }
}

/// "RSS…19X": nell'interfaccia codice fiscale e ricetta non si vedono mai per intero.
fn mascherato(s: &str) -> String {
    let c: Vec<char> = s.chars().collect();
    if c.len() < 8 {
        return "…".into();
    }
    format!("{}…{}", c[..3].iter().collect::<String>(), c[c.len() - 3..].iter().collect::<String>())
}

const GIORNI: [&str; 7] = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"];

#[derive(Serialize)]
struct EsitoUi {
    ora: String,
    controllo: bool,
    attuale: Option<Visita>,
    date: usize,
    buone: Vec<Data>,
    area_incompleta: bool,
    errore: Option<String>,
    nota: Option<String>,
    note_visita: Vec<String>,
    cosa: String,
    solo_proposta: bool,
}

#[derive(Serialize)]
struct Scheda {
    id: u64,
    nome: String,
    cf: String,
    nre: String,
    dove: String,
    valore: String,
    evita: Vec<u32>,
    zona: String,
    giorni: String,
    attiva: bool,
    /// Mai prenotata: l'app cerca il primo posto libero e lo prenota con "Prenota qui".
    nuova: bool,
    /// "controllo", "spostamento", "prenotazione" o "disdetta", se c'e' qualcosa in corso.
    in_corso: Option<Attivita>,
    /// Un'operazione dall'esito incerto, da controllare sul portale.
    incerta: Option<String>,
    prossimo: Option<String>,
    esito: Option<EsitoUi>,
}

fn scheda(r: &Ricetta, in_corso: Option<Attivita>) -> Scheda {
    let zona = match &r.esito {
        _ if r.nuova && controlli::della_prenotazione(&r.dove, &r.valore) => {
            "dove propone il CUP, finché non è prenotata".into()
        }
        Some(e) if !e.zona.is_empty() => e.zona.clone(),
        _ => Dove::da_scelta(&r.dove, &r.valore).map(|d| d.descrizione(None)).unwrap_or_default(),
    };
    let mut evita = r.evita.clone();
    evita.sort();
    let giorni = if evita.is_empty() {
        "tutti i giorni".into()
    } else {
        format!(
            "niente {}",
            evita.iter().filter_map(|g| GIORNI.get(*g as usize)).copied().collect::<Vec<_>>().join(", ")
        )
    };
    Scheda {
        id: r.id,
        nome: r.nome.clone(),
        cf: mascherato(&r.cf),
        nre: mascherato(&r.nre),
        dove: r.dove.clone(),
        valore: r.valore.clone(),
        evita,
        zona,
        giorni,
        attiva: r.attiva,
        nuova: r.nuova,
        in_corso,
        incerta: r.incerta.clone(),
        prossimo: controlli::prossimo(r, adesso()).map(ora),
        esito: r.esito.as_ref().map(|e| EsitoUi {
            ora: ora(e.ts),
            controllo: e.controllo,
            attuale: e.attuale.clone(),
            date: e.date,
            buone: e.buone.clone(),
            area_incompleta: e.area_incompleta,
            errore: e.errore.clone(),
            nota: e.nota.clone(),
            note_visita: e.note_visita.clone(),
            cosa: e.cosa.clone(),
            solo_proposta: e.solo_proposta,
        }),
    }
}

/// Salva e dice all'interfaccia di ridisegnarsi.
fn salva(app: &AppHandle, stato: &Stato, dati: &archivio::Dati) -> Result<(), String> {
    let esito = stato.salva(dati);
    let _ = app.emit("ricette", ());
    esito
}

#[derive(Serialize)]
struct Elenco {
    ricette: Vec<Scheda>,
    /// Un salvataggio non riuscito, da mostrare finche' non ne riesce uno.
    avviso: Option<String>,
}

#[tauri::command]
fn ricette(stato: Condiviso) -> Elenco {
    let in_corso = stato.in_corso();
    let ricette = stato.dati().ricette.iter().map(|r| scheda(r, in_corso.get(&r.id).copied())).collect();
    Elenco { ricette, avviso: stato.avviso() }
}

fn nome_pulito(nome: &str) -> Result<String, String> {
    let n = nome.split_whitespace().collect::<Vec<_>>().join(" ");
    if n.is_empty() {
        return Err("Dai un nome alla ricetta, per esempio Papà o Ecografia.".into());
    }
    Ok(n.chars().take(30).collect())
}

/// Cosa legge il portale quando si aggiunge una ricetta, senza aprire gli appuntamenti (non blocca date).
enum Letta {
    /// La prenotazione attuale.
    Prenotata(Box<Prenotazione>),
    /// Mai prenotata (o tutte le prenotazioni disdette): le prestazioni del carrello.
    Nuova(Vec<String>),
}

/// Recupera Prenotazioni; se il portale non trova prenotazioni, il passo "Ricerca" di una prenotazione nuova.
fn leggi_ricetta(cf: &str, nre: &str) -> Result<Letta, String> {
    let testo = |e: Errore| e.to_string();
    match Sessione::nuova(cf, nre).map_err(testo)?.elenco() {
        Ok(e) => Ok(Letta::Prenotata(Box::new(e.attuale))),
        Err(e) if matches!(e, Errore::NonTrovata(_)) || e.solo_disdette() => {
            let mut s = Sessione::nuova(cf, nre).map_err(testo)?;
            match s.ricetta() {
                Ok(_) => Ok(Letta::Nuova(s.prestazioni().to_vec())),
                Err(Errore::NonTrovata(m)) => Err(format!(
                    "Il portale non trova prenotazioni per questa ricetta e non la accetta per prenotarla ({m}). \
                     Controlla codice fiscale e numero della ricetta."
                )),
                // l'elenco non la trova, ma la Ricerca la dice prenotata: il portale si contraddice, meglio riprovare
                Err(Errore::GiaPrenotata(m)) => Err(format!(
                    "Il portale non mostra la prenotazione ma dice che la ricetta è già prenotata ({m}). Riprova tra \
                     qualche minuto."
                )),
                Err(e) => Err(e.to_string()),
            }
        }
        Err(e) => Err(e.to_string()),
    }
}

/// Una ricetta nuova: prima legge dal portale la prenotazione o, se non c'e', la ricetta da prenotare (solo lettura,
/// non apre gli appuntamenti), poi la salva. Ritorna il messaggio per l'utente.
#[tauri::command]
#[allow(clippy::too_many_arguments)]
async fn aggiungi(
    app: AppHandle,
    stato: Condiviso<'_>,
    nome: String,
    cf: String,
    nre: String,
    dove: String,
    valore: String,
    evita: Vec<u32>,
    attiva: bool,
) -> Result<String, String> {
    let nome = nome_pulito(&nome)?;
    Dove::da_scelta(&dove, &valore).map_err(|e| e.to_string())?;
    let (cf, nre) = credenziali(&cf, &nre).map_err(|e| e.to_string())?;
    if stato.dati().ricette.iter().any(|r| r.nre == nre) {
        return Err("Questa ricetta c'è già.".into());
    }
    let (c, n) = (cf.clone(), nre.clone());
    let letta = tauri::async_runtime::spawn_blocking(move || leggi_ricetta(&c, &n))
        .await
        .map_err(|_| "errore interno".to_string())??;
    let (nuova, attuale, cosa) = match letta {
        Letta::Prenotata(a) => (false, Some(*a), String::new()),
        Letta::Nuova(nomi) => (true, None, nomi.join(" + ")),
    };
    // mai prenotata: una zona "della prenotazione" vale come "dove propone il CUP" finche' non e' prenotata
    let zona = if nuova && controlli::della_prenotazione(&dove, &valore) {
        Dove::Tutte
    } else {
        Dove::da_scelta(&dove, &valore).map_err(|e| e.to_string())?
    };
    let mut d = stato.dati();
    if d.ricette.iter().any(|r| r.nre == nre) {
        return Err("Questa ricetta c'è già.".into());
    }
    // riaggiunta subito dopo averla eliminata: i 20 minuti dall'ultimo controllo valgono ancora
    let ultimo_tentativo = d.ultimo_eliminata(&nre, adesso(), INTERVALLO);
    d.prossimo_id += 1;
    let id = d.prossimo_id;
    d.ricette.push(Ricetta {
        id,
        nome,
        cf,
        nre,
        dove,
        valore,
        evita: evita.into_iter().filter(|g| *g < 7).collect(),
        attiva,
        ultimo_tentativo,
        esito: Some(Esito {
            ts: adesso(),
            attuale: attuale.as_ref().map(Visita::da),
            zona: zona.descrizione(attuale.as_ref()),
            cosa,
            ..Default::default()
        }),
        notificate: vec![],
        assenze: 0,
        operazione: None,
        incerta: None,
        nuova,
    });
    stato.registro.scrivi(&format!(
        "ricetta {id}: aggiunta{}{}",
        if nuova { " (mai prenotata)" } else { "" },
        if attiva { "" } else { " (in pausa)" }
    ));
    salva(&app, &stato, &d)?;
    Ok(if !nuova {
        "Ricetta aggiunta.".into()
    } else if attiva {
        "Ricetta aggiunta. Non è ancora prenotata: l'app cerca il primo posto libero e te lo propone, e prenota solo \
         se tocchi «Prenota qui»."
            .into()
    } else {
        "Ricetta aggiunta, in pausa. Non è ancora prenotata: quando riprendi i controlli l'app cerca il primo posto \
         libero."
            .into()
    })
}

#[tauri::command]
fn modifica(
    app: AppHandle,
    stato: Condiviso,
    id: u64,
    nome: String,
    dove: String,
    valore: String,
    evita: Vec<u32>,
) -> Result<(), String> {
    let nome = nome_pulito(&nome)?;
    Dove::da_scelta(&dove, &valore).map_err(|e| e.to_string())?;
    let mut d = stato.dati();
    let r = d.ricetta(id)?;
    let evita: Vec<u32> = evita.into_iter().filter(|g| *g < 7).collect();
    if (&r.dove, &r.valore, &r.evita) != (&dove, &valore, &evita) {
        // la zona letta con la prenotazione non vale piu': le date buone si rivedono al prossimo controllo
        if let Some(e) = r.esito.as_mut() {
            e.zona.clear();
            e.buone.clear();
        }
    }
    (r.nome, r.dove, r.valore, r.evita) = (nome, dove, valore, evita);
    stato.registro.scrivi(&format!("ricetta {id}: modificata"));
    salva(&app, &stato, &d)
}

#[tauri::command]
fn elimina(app: AppHandle, stato: Condiviso, id: u64) -> Result<(), String> {
    let in_corso = controlli::blocca(&stato.in_corso);
    if in_corso.get(&id).is_some_and(|a| a.operazione()) {
        return Err("Aspetta la fine dell'operazione in corso su questa ricetta.".into());
    }
    let mut d = stato.dati();
    controlli::blocca(&stato.tenute).remove(&id);
    d.elimina(id, adesso(), INTERVALLO);
    stato.registro.scrivi(&format!("ricetta {id}: eliminata"));
    salva(&app, &stato, &d)
}

#[tauri::command]
fn attiva(app: AppHandle, stato: Condiviso, id: u64, si: bool) -> Result<(), String> {
    let mut d = stato.dati();
    let r = d.ricetta(id)?;
    r.attiva = si;
    r.assenze = 0;
    stato.registro.scrivi(&format!("ricetta {id}: {}", if si { "controlli ripresi" } else { "controlli in pausa" }));
    salva(&app, &stato, &d)
}

/// Mostra la notifica di un'operazione finita e dice all'interfaccia di ridisegnarsi.
fn finita(app: &AppHandle, fine: Result<Fine, String>) -> Result<String, String> {
    let _ = app.emit("ricette", ());
    let fine = fine?;
    if let Some(n) = &fine.notifica {
        avvisa(app, &n.titolo, &n.testo, &format!("ricetta {}, {}", n.ricetta, n.tipo));
    }
    if fine.ok {
        Ok(fine.messaggio)
    } else {
        Err(fine.messaggio)
    }
}

/// Sposta la prenotazione su una delle date buone trovate (l'utente l'ha confermato nell'interfaccia).
#[tauri::command]
async fn sposta(app: AppHandle, stato: Condiviso<'_>, id: u64, chiave: String) -> Result<String, String> {
    let st = stato.inner().clone();
    let h = app.clone();
    let fine = tauri::async_runtime::spawn_blocking(move || {
        let motore = |r: &Ricetta, tenuta: Option<Sessione>, fasi: operazioni::Fasi| {
            let (zona, cal) = zona_di(r)?;
            let (cf, nre) = (r.cf.clone(), r.nre.clone());
            cup_core::operazioni::sposta(
                &|attesa| Sessione::con_limite(&cf, &nre, attesa),
                tenuta,
                &chiave,
                &zona,
                &cal,
                false,
                &Attese::default(),
                fasi,
            )
        };
        operazioni::sposta(&st, id, &chiave, &motore, &|| {
            let _ = h.emit("ricette", ());
        })
    })
    .await
    .map_err(|_| "errore interno".to_string())?;
    finita(&app, fine)
}

/// Prenota una ricetta mai prenotata su una delle date buone trovate (l'utente l'ha confermato nell'interfaccia).
#[tauri::command]
async fn prenota(app: AppHandle, stato: Condiviso<'_>, id: u64, chiave: String) -> Result<String, String> {
    let st = stato.inner().clone();
    let h = app.clone();
    let fine = tauri::async_runtime::spawn_blocking(move || {
        let motore = |r: &Ricetta, tenuta: Option<Sessione>, fasi: operazioni::Fasi| {
            let (zona, cal) = zona_di(r)?;
            let (cf, nre) = (r.cf.clone(), r.nre.clone());
            cup_core::operazioni::prenota(
                &|attesa| Sessione::con_limite(&cf, &nre, attesa),
                tenuta,
                &chiave,
                &zona,
                &cal,
                false,
                &Attese::default(),
                fasi,
            )
        };
        operazioni::prenota(&st, id, &chiave, &motore, &|| {
            let _ = h.emit("ricette", ());
        })
    })
    .await
    .map_err(|_| "errore interno".to_string())?;
    finita(&app, fine)
}

/// Disdice la prenotazione mostrata (l'utente l'ha confermato nell'interfaccia).
#[tauri::command]
async fn disdici(app: AppHandle, stato: Condiviso<'_>, id: u64) -> Result<String, String> {
    let st = stato.inner().clone();
    let h = app.clone();
    let fine = tauri::async_runtime::spawn_blocking(move || {
        let motore = |r: &Ricetta, quando, luogo: Option<&str>, fasi: operazioni::Fasi| {
            let (cf, nre) = (r.cf.clone(), r.nre.clone());
            cup_core::operazioni::disdici(
                &|attesa| Sessione::con_limite(&cf, &nre, attesa),
                quando,
                luogo,
                false,
                &Attese::default(),
                fasi,
            )
        };
        operazioni::disdici(&st, id, &motore, &|| {
            let _ = h.emit("ricette", ());
        })
    })
    .await
    .map_err(|_| "errore interno".to_string())?;
    finita(&app, fine)
}

/// L'utente ha controllato sul portale l'esito di un'operazione incerta: si possono fare di nuovo operazioni.
#[tauri::command]
fn chiudi_incerta(app: AppHandle, stato: Condiviso, id: u64) -> Result<(), String> {
    let mut d = stato.dati();
    d.ricetta(id)?.incerta = None;
    stato.registro.scrivi(&format!("ricetta {id}: esito incerto controllato dall'utente"));
    salva(&app, &stato, &d)
}

#[derive(Serialize)]
struct Impostazioni {
    avvio_automatico: bool,
    protezione: Protezione,
    messo_da_parte: Option<String>,
    registro: String,
}

#[tauri::command]
fn impostazioni(app: AppHandle, stato: Condiviso) -> Impostazioni {
    Impostazioni {
        avvio_automatico: app.autolaunch().is_enabled().unwrap_or(false),
        protezione: stato.archivio.protezione,
        messo_da_parte: stato.archivio.messo_da_parte.as_ref().map(|p| p.display().to_string()),
        registro: stato.registro.percorso().display().to_string(),
    }
}

/// Le ultime righe del registro, dalla piu' recente.
#[tauri::command]
fn registro(stato: Condiviso) -> Vec<String> {
    let mut righe = stato.registro.ultime(40);
    righe.reverse();
    righe
}

#[tauri::command]
fn imposta_avvio(app: AppHandle, stato: Condiviso, si: bool) -> Result<(), String> {
    let a = app.autolaunch();
    if si { a.enable() } else { a.disable() }.map_err(|e| format!("Non riesco a cambiare l'avvio automatico: {e}"))?;
    stato.registro.scrivi(if si { "avvio automatico acceso" } else { "avvio automatico spento" });
    Ok(())
}

#[tauri::command]
fn aggiornamenti(agg: Canale) -> aggiornamenti::Info {
    agg.info()
}

#[tauri::command]
fn imposta_aggiornamenti(stato: Condiviso, agg: Canale, si: bool) -> Result<(), String> {
    agg.imposta(si).map_err(|e| format!("Non riesco a salvare la scelta: {e}"))?;
    stato.registro.scrivi(if si { "aggiornamenti automatici accesi" } else { "aggiornamenti automatici spenti" });
    Ok(())
}

/// Controlla subito se c'e' una versione nuova. Ritorna il messaggio per l'utente.
#[tauri::command]
async fn controlla_aggiornamenti(app: AppHandle, stato: Condiviso<'_>, agg: Canale<'_>) -> Result<String, String> {
    let (st, ag) = (stato.inner().clone(), agg.inner().clone());
    let v = tauri::async_runtime::spawn_blocking(move || ag.controlla(&app, &*st))
        .await
        .map_err(|_| "errore interno".to_string())??;
    Ok(match v {
        Some(v) => format!("C'è la versione {v}."),
        None => "È l'ultima versione.".into(),
    })
}

/// "Aggiorna": installa subito la versione nuova, senza aspettare il rodaggio (mai durante un'operazione). Se va,
/// l'app si riavvia e non torna qui.
#[tauri::command]
async fn aggiorna_ora(app: AppHandle, stato: Condiviso<'_>, agg: Canale<'_>) -> Result<(), String> {
    let (st, ag) = (stato.inner().clone(), agg.inner().clone());
    tauri::async_runtime::spawn_blocking(move || {
        if ag.controlla(&app, &*st)?.is_none() {
            return Err("È già l'ultima versione.".to_string());
        }
        ag.installa(&app, &*st, false)
    })
    .await
    .map_err(|_| "errore interno".to_string())?
}

/// Esce, ma non durante uno spostamento, una prenotazione o una disdetta: a meta' l'esito resterebbe da verificare a
/// mano.
fn esci_se_libera(app: &AppHandle) {
    if app.try_state::<Arc<Stato>>().is_some_and(|s| s.in_chiusura()) {
        avvisa(
            app,
            "CUP Piemonte si sta aggiornando",
            "L'app si chiude e si riapre da sola tra poco: lasciala finire.",
            "uscita rimandata: aggiornamento in corso",
        );
        return;
    }
    let occupata = app.try_state::<Arc<Stato>>().is_some_and(|s| s.in_corso().values().any(|a| a.operazione()));
    if occupata {
        mostra(app);
        avvisa(
            app,
            "CUP Piemonte: aspetta un momento",
            "C'è uno spostamento, una prenotazione o una disdetta in corso: l'app esce quando ha verificato l'esito \
             sul portale. Riprova tra qualche minuto.",
            "uscita rimandata: operazione in corso",
        );
        return;
    }
    app.exit(0);
}

fn mostra(app: &AppHandle) {
    // se l'avvio non e' riuscito c'e' solo la finestra di errore: l'interfaccia non funzionerebbe
    if app.try_state::<Arc<Stato>>().is_none() {
        return;
    }
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// Mostra una notifica; nel registro va `per_registro`, mai titolo e testo (nome della ricetta, date della visita).
fn avvisa(app: &AppHandle, titolo: &str, testo: &str, per_registro: &str) {
    let esito = app.notification().builder().title(titolo).body(testo).show();
    if let Some(stato) = app.try_state::<Arc<Stato>>() {
        stato.registro.scrivi(&match esito {
            Ok(()) => format!("notifica: {per_registro}"),
            Err(e) => format!("notifica non mostrata ({e}): {per_registro}"),
        });
    }
}

/// Lo stato dell'app, o perche' non si puo' aprire (portachiavi che non risponde, archivio di un'altra versione...).
fn prepara(app: &tauri::App, registro: Registro) -> Result<(Arc<Stato>, Vec<Notifica>), (Registro, String)> {
    let cartella = match app.path().app_data_dir() {
        Ok(c) => c,
        Err(e) => return Err((registro, format!("Non trovo la cartella dei dati dell'app: {e}"))),
    };
    let archivio = match Archivio::apri(&cartella) {
        Ok(a) => a,
        Err(e) => return Err((registro, e)),
    };
    if archivio.messo_da_parte.is_some() {
        registro.scrivi("archivio illeggibile messo da parte, ricomincio da capo");
    }
    let mut dati = match archivio.leggi() {
        Ok(d) => d,
        Err(e) => return Err((registro, e)),
    };
    // un'operazione rimasta a meta' (l'app si e' fermata dopo la Conferma): esito incerto, da dire all'utente
    let interrotte = operazioni::riprendi(&mut dati);
    if !interrotte.is_empty() {
        for n in &interrotte {
            registro.scrivi(&format!("ricetta {}: operazione interrotta dopo la conferma, esito incerto", n.ricetta));
        }
        if let Err(e) = archivio.scrivi(&dati) {
            registro.scrivi(&format!("salvataggio non riuscito: {e}"));
        }
    }
    let stato = Arc::new(Stato::new(archivio, registro, dati));
    Ok((stato, interrotte))
}

fn main() {
    // l'avviso "resto nella barra di sistema" una volta sola per avvio
    let avvisato = AtomicBool::new(false);
    tauri::Builder::default()
        // una sola copia dell'app: due thread di controlli bloccherebbero due date e si cancellerebbero i dati
        .plugin(tauri_plugin_single_instance::init(|app, args, _| {
            // la seconda copia partita con il computer non deve aprire la finestra
            if !args.iter().any(|a| a == "--nascosta") {
                mostra(app);
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec!["--nascosta"])))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            let registro = Registro::new(&app.path().app_log_dir()?);
            registro.scrivi(&format!("avvio, versione {}", app.package_info().version));
            let (stato, interrotte) = match prepara(app, registro) {
                Ok(s) => s,
                Err((registro, e)) => {
                    // senza console (in release non c'e') l'utente deve vederlo: una finestra, poi si esce
                    registro.scrivi(&format!("avvio non riuscito: {e}"));
                    // una versione difettosa (per esempio che non apre le ricette) si corregge da sola: se c'e' una
                    // versione nuova si installa subito
                    let installando = app.path().app_data_dir().ok().map(|cartella| {
                        let v = app.package_info().version.to_string();
                        let bt = tauri::utils::platform::bundle_type();
                        aggiornamenti::soccorso(app.handle().clone(), registro, &cartella, v, bt)
                    });
                    let h = app.handle().clone();
                    app.dialog()
                        .message(e)
                        .title("CUP Piemonte non riesce a partire")
                        .kind(MessageDialogKind::Error)
                        .show(move |_| {
                            // a meta' di un aggiornamento non si esce: l'app si riavvia da sola
                            if !installando.is_some_and(|i| i.load(std::sync::atomic::Ordering::SeqCst)) {
                                h.exit(1)
                            }
                        });
                    return Ok(());
                }
            };
            app.manage(stato.clone());

            // aggiornamenti: com'e' andata l'installazione di prima del riavvio, se ce n'era una
            let installata_con = tauri::utils::platform::bundle_type();
            let versione = app.package_info().version.to_string();
            let agg = Arc::new(Aggiornamenti::new(&app.path().app_data_dir()?, versione, installata_con));
            stato.registro.scrivi(&format!(
                "aggiornamenti: installazione {}, {}",
                agg.installata_con,
                match (agg.modo, agg.canale().spenti) {
                    (aggiornamenti::Modo::NonDisponibile, _) => "non si aggiorna",
                    (aggiornamenti::Modo::ARichiesta, _) => "si aggiorna con Aggiorna",
                    (_, true) => "automatici spenti",
                    _ => "si aggiorna da sola",
                }
            ));
            let mut nascosta = std::env::args().any(|a| a == "--nascosta");
            match agg.dopo_avvio() {
                Ok(Some(Avvio::Riuscito { da, a, automatica })) => {
                    stato.registro.scrivi(&format!("aggiornamenti: aggiornata dalla versione {da} alla {a}"));
                    avvisa(
                        app.handle(),
                        &format!("CUP Piemonte aggiornata alla versione {a}"),
                        "I controlli continuano come prima. Le novità sono nella pagina dell'app.",
                        &format!("aggiornata alla versione {a}"),
                    );
                    // installata da sola, magari di notte: come all'avvio con il computer; chiesta con "Aggiorna",
                    // la finestra torna (anche se l'app era partita con il computer)
                    nascosta = automatica;
                }
                Ok(Some(Avvio::NonRiuscito { a, volte })) => {
                    stato.registro.scrivi(&format!("aggiornamenti: installazione della versione {a} non riuscita ({volte})"));
                    if volte >= aggiornamenti::TENTATIVI {
                        avvisa(
                            app.handle(),
                            "CUP Piemonte non si è aggiornata",
                            &format!(
                                "L'installazione della versione {a} non è riuscita: l'app continua con questa versione. \
                                 Puoi riprovare da ⚙ o scaricarla dalla pagina dell'app."
                            ),
                            &format!("aggiornamento alla versione {a} non riuscito"),
                        );
                    }
                }
                Ok(None) => {}
                Err(e) => stato.registro.scrivi(&format!("aggiornamenti: stato non salvato: {e}")),
            }
            app.manage(agg.clone());

            let apri = MenuItem::with_id(app, "apri", "Apri", true, None::<&str>)?;
            let esci = MenuItem::with_id(app, "esci", "Esci (ferma i controlli)", true, None::<&str>)?;
            TrayIconBuilder::with_id("cup")
                .icon(app.default_window_icon().cloned().ok_or("icona mancante")?)
                .tooltip("CUP Piemonte")
                .menu(&Menu::with_items(app, &[&apri, &esci])?)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, ev| match ev.id.as_ref() {
                    "apri" => mostra(app),
                    "esci" => esci_se_libera(app),
                    _ => {}
                })
                .on_tray_icon_event(|tray, ev| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left, button_state: MouseButtonState::Up, ..
                    } = ev
                    {
                        mostra(tray.app_handle());
                    }
                })
                .build(app)?;

            // la finestra nasce nascosta: si mostra, tranne quando l'app parte con il computer (o quando c'e' da
            // dire che un'operazione e' rimasta a meta')
            if !interrotte.is_empty() || !nascosta {
                mostra(app.handle());
            }
            for n in &interrotte {
                avvisa(app.handle(), &n.titolo, &n.testo, &format!("ricetta {}, {}", n.ricetta, n.tipo));
            }
            // archivio, icona e finestra pronti (la prova degli installer su GitHub aspetta questa riga)
            stato.registro.scrivi("avvio completato");
            let (h1, h2) = (app.handle().clone(), app.handle().clone());
            let (h3, h4) = (app.handle().clone(), app.handle().clone());
            let amb: Arc<dyn aggiornamenti::Ambiente> = stato.clone();
            aggiornamenti::avvia(app.handle().clone(), amb, agg.clone(), move |titolo, testo, registro| {
                avvisa(&h3, titolo, testo, registro)
            });
            // solo sulle macchine di GitHub (CI=true e CUP_PROVA_AGGIORNAMENTO): la prova dell'aggiornamento
            let (st, ag) = (stato.clone(), agg);
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_secs(5));
                ag.prova_ci(&h4, &*st);
            });
            controlli::avvia(
                stato,
                move |notifiche| {
                    for n in notifiche {
                        avvisa(&h1, &n.titolo, &n.testo, &format!("ricetta {}, {}", n.ricetta, n.tipo));
                    }
                },
                move || {
                    let _ = h2.emit("ricette", ());
                },
            );
            Ok(())
        })
        .on_window_event(move |w, ev| {
            if let WindowEvent::CloseRequested { api, .. } = ev {
                // chiudere la finestra non ferma i controlli: si esce dal menu dell'icona
                api.prevent_close();
                let _ = w.hide();
                if !avvisato.swap(true, Ordering::Relaxed) {
                    avvisa(
                        w.app_handle(),
                        "CUP Piemonte continua a controllare",
                        "L'app resta nella barra di sistema. Per chiuderla: icona, poi Esci.",
                        "l'app resta nella barra di sistema",
                    );
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            ricette,
            aggiungi,
            modifica,
            elimina,
            attiva,
            impostazioni,
            imposta_avvio,
            registro,
            sposta,
            prenota,
            disdici,
            chiudi_incerta,
            aggiornamenti,
            imposta_aggiornamenti,
            controlla_aggiornamenti,
            aggiorna_ora
        ])
        .run(tauri::generate_context!())
        .expect("avvio dell'app non riuscito");
}
