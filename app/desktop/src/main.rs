// in release niente finestra del terminale su Windows
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod archivio;
mod controlli;
mod operazioni;
mod registro;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use chrono::{Local, TimeZone};
use cup_core::analisi::credenziali;
use cup_core::operazioni::Attese;
use cup_core::{Dove, Errore, Sessione};
use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_notification::NotificationExt;

use archivio::{Archivio, Data, Esito, Protezione, Ricetta, Visita};
use controlli::{adesso, zona_di, Attivita, Notifica, Stato, INTERVALLO};
use operazioni::Fine;
use registro::Registro;

type Condiviso<'a> = State<'a, Arc<Stato>>;

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
    /// "controllo", "spostamento" o "disdetta", se c'e' qualcosa in corso.
    in_corso: Option<Attivita>,
    /// Un'operazione dall'esito incerto, da controllare sul portale.
    incerta: Option<String>,
    prossimo: Option<String>,
    esito: Option<EsitoUi>,
}

fn scheda(r: &Ricetta, in_corso: Option<Attivita>) -> Scheda {
    let zona = match &r.esito {
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

/// Una ricetta nuova: prima legge la prenotazione dal portale (solo lettura, non apre "Sposta"), poi la salva.
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
) -> Result<(), String> {
    let nome = nome_pulito(&nome)?;
    let zona = Dove::da_scelta(&dove, &valore).map_err(|e| e.to_string())?;
    let (cf, nre) = credenziali(&cf, &nre).map_err(|e| e.to_string())?;
    if stato.dati().ricette.iter().any(|r| r.nre == nre) {
        return Err("Questa ricetta c'è già.".into());
    }
    let (c, n) = (cf.clone(), nre.clone());
    let elenco = tauri::async_runtime::spawn_blocking(move || Sessione::nuova(&c, &n)?.elenco())
        .await
        .map_err(|_| "errore interno".to_string())?
        .map_err(|e| match e {
            Errore::NonTrovata(m) => format!(
                "Il portale non trova prenotazioni per questa ricetta ({m}). Per ora l'app segue solo ricette già \
                 prenotate."
            ),
            e => e.to_string(),
        })?;
    let a = &elenco.attuale;
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
            attuale: Some(Visita::da(a)),
            zona: zona.descrizione(Some(a)),
            ..Default::default()
        }),
        notificate: vec![],
        assenze: 0,
        operazione: None,
        incerta: None,
    });
    stato.registro.scrivi(&format!("ricetta {id}: aggiunta{}", if attiva { "" } else { " (in pausa)" }));
    salva(&app, &stato, &d)
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
    if matches!(in_corso.get(&id), Some(Attivita::Spostamento | Attivita::Disdetta)) {
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

/// Esce, ma non durante uno spostamento o una disdetta: a meta' l'esito resterebbe da verificare a mano.
fn esci_se_libera(app: &AppHandle) {
    let occupata = app
        .try_state::<Arc<Stato>>()
        .is_some_and(|s| s.in_corso().values().any(|a| matches!(a, Attivita::Spostamento | Attivita::Disdetta)));
    if occupata {
        mostra(app);
        avvisa(
            app,
            "CUP Piemonte: aspetta un momento",
            "C'è uno spostamento o una disdetta in corso: l'app esce quando ha verificato l'esito sul portale. \
             Riprova tra qualche minuto.",
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
        .setup(|app| {
            let registro = Registro::new(&app.path().app_log_dir()?);
            registro.scrivi(&format!("avvio, versione {}", app.package_info().version));
            let (stato, interrotte) = match prepara(app, registro) {
                Ok(s) => s,
                Err((registro, e)) => {
                    // senza console (in release non c'e') l'utente deve vederlo: una finestra, poi si esce
                    registro.scrivi(&format!("avvio non riuscito: {e}"));
                    let h = app.handle().clone();
                    app.dialog()
                        .message(e)
                        .title("CUP Piemonte non riesce a partire")
                        .kind(MessageDialogKind::Error)
                        .show(move |_| h.exit(1));
                    return Ok(());
                }
            };
            app.manage(stato.clone());

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
            if !interrotte.is_empty() || !std::env::args().any(|a| a == "--nascosta") {
                mostra(app.handle());
            }
            for n in &interrotte {
                avvisa(app.handle(), &n.titolo, &n.testo, &format!("ricetta {}, {}", n.ricetta, n.tipo));
            }
            let (h1, h2) = (app.handle().clone(), app.handle().clone());
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
            disdici,
            chiudi_incerta
        ])
        .run(tauri::generate_context!())
        .expect("avvio dell'app non riuscito");
}
