//! Pagine finte del portale per i test, con lo stesso markup dei test del bot (telegram-bot/tests).

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::thread;

use crate::analisi::{A, L, RIEPILOGO};

/// (riga iniziale, intestazioni in minuscolo, corpo) di una richiesta ricevuta dal finto portale.
pub type Vista = (String, String, String);

/// Un finto portale su 127.0.0.1: risponde con le pagine che `risposte` costruisce dal suo indirizzo, una
/// per richiesta, e restituisce le richieste ricevute. Il codice 0 e' una risposta che non arriva in tempo.
pub fn portale(risposte: impl FnOnce(&str) -> Vec<(u16, String)>) -> (String, thread::JoinHandle<Vec<Vista>>) {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", l.local_addr().unwrap());
    let risposte = risposte(&base);
    let h = thread::spawn(move || {
        let mut viste = vec![];
        for (codice, corpo) in risposte {
            let (mut c, _) = l.accept().unwrap();
            let mut r = BufReader::new(c.try_clone().unwrap());
            let (mut riga, mut intest, mut lunghezza) = (String::new(), String::new(), 0);
            r.read_line(&mut riga).unwrap();
            loop {
                let mut h = String::new();
                r.read_line(&mut h).unwrap();
                if h.trim().is_empty() {
                    break;
                }
                let h = h.to_lowercase();
                if let Some(v) = h.strip_prefix("content-length:") {
                    lunghezza = v.trim().parse().unwrap();
                }
                intest.push_str(&h);
            }
            let mut b = vec![0; lunghezza];
            r.read_exact(&mut b).unwrap();
            viste.push((riga.trim().to_string(), intest, String::from_utf8(b).unwrap()));
            if codice == 0 {
                thread::sleep(std::time::Duration::from_millis(1500));
                continue;
            }
            write!(
                c,
                "HTTP/1.1 {codice} X\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n\
                 Set-Cookie: JSESSIONID=abc; Path=/\r\nConnection: close\r\n\r\n{corpo}",
                corpo.len()
            )
            .unwrap();
        }
        viste
    });
    (base, h)
}
/// I campi di un POST ricevuto, nell'ordine.
pub fn campi(corpo: &str) -> Vec<(String, String)> {
    serde_urlencoded::from_str(corpo).unwrap()
}

/// Il valore del campo `k` di un POST ricevuto.
pub fn valore(corpo: &str, k: &str) -> Option<String> {
    campi(corpo).into_iter().find(|(n, _)| n == k).map(|(_, v)| v)
}

/// Un appuntamento come lo disegna il portale. `citta` e' la fine dell'indirizzo ("TORINO (TO)"); con
/// `seleziona` ha il suo pulsante "Seleziona", come le date di "Appuntamenti Disponibili".
pub fn appuntamento(quando: &str, sede: &str, amb: &str, citta: &str, seleziona: Option<&str>) -> String {
    let pulsante = seleziona.map_or(String::new(), |id| {
        format!(
            r#"<div class=" btn btn-primary iconButton icon-ok wideButtons mb-5" id="{id}" onmouseover=""><span><span><button type="button"><span>Seleziona</span></button></span></span></div>"#
        )
    });
    format!(
        r#"<div class="captionAppointment-what"><span>Cosa:</span> </div><div class="captionAppointment-desc"><span id="d">VISITA DI ESEMPIO - 11.11</span><span> (PRENOTABILE)</span> <br /> </div><span>Quando:</span><span class="fw-b">{quando}</span><span>alle ore</span><span class="fw-b">09:00</span><span>Dove: </span><br /><span>Mappa</span> </div> <div style="flex-grow:1;"> <div> </div><div><span id="s">{sede}</span> </div> <div><span id="a">{amb}</span> </div> <div class="unita-address" data-address="Via Roma, 1"><span>Via Roma, 1</span><span> - {citta}</span> </div></div>{pulsante}"#
    )
}

/// Un modulo JSF con il suo stato; `azione` e' dove il portale dice di mandare il POST.
pub fn modulo(id: &str, azione: &str, corpo: &str) -> String {
    format!(
        r#"<form id="{id}" method="post"><input type="hidden" name="{id}" value="{id}" /><input type="hidden" name="javax.faces.encodedURL" value="{azione}" /><input type="hidden" name="ice.window" value="w1" /><input type="hidden" name="ice.view" value="v1" />{corpo}<input type="hidden" name="javax.faces.ViewState" value="vs1" /></form>"#
    )
}

/// La risposta di Recupera Prenotazioni con una prenotazione e il suo "Sposta appuntamento".
pub fn elenco_prenotato(quando: &str, sede: &str, citta: &str) -> String {
    format!(
        r#"<partial-response><![CDATA[<span>Stato:</span> PRENOTATO {}<div id="{L}:righe:0:spostaButton">Sposta appuntamento</div><div id="{L}:righe:0:disdiciButton">Disdici</div>]]></partial-response>"#,
        appuntamento(quando, sede, "AMB 1", citta, None)
    )
}

/// Recupera Prenotazioni con queste righe (stato, giorno, sede, ambulatorio, prestazione), tutte alle 09:00 e a
/// Torino, ognuna con i suoi pulsanti.
pub fn elenco(righe: &[(&str, &str, &str, &str, &str)]) -> String {
    let righe: String = righe
        .iter()
        .enumerate()
        .map(|(i, (stato, quando, sede, amb, cosa))| {
            let a = appuntamento(quando, sede, amb, "TORINO (TO)", None).replace("VISITA DI ESEMPIO - 11.11", cosa);
            format!(
                r#"<span>Stato:</span> {stato} {a}<div id="{L}:righe:{i}:spostaButton">Sposta appuntamento</div><div id="{L}:righe:{i}:disdiciButton">Disdici</div>"#
            )
        })
        .collect();
    format!("<partial-response><![CDATA[{righe}]]></partial-response>")
}

/// Il Riepilogo di uno spostamento: le prestazioni, la data alle 09:00 e subito dopo sede e ambulatorio, il pulsante
/// "Conferma"; con `note`, anche le note dell'appuntamento e "Conferma presa visione".
pub fn riepilogo(azione: &str, quando: &str, sede: &str, amb: &str, cose: &[&str], note: bool) -> String {
    let note = if note {
        format!(
            r#"<div id="noteDialog"><h3 class="popoverTitle">Note</h3><p>Presentarsi a digiuno</p><p>Portare la ricetta</p></div><div id="{RIEPILOGO}:_t42" class="btn"><button aria-describedby="Conferma presa visione">Conferma presa visione</button></div>"#
        )
    } else {
        String::new()
    };
    let cose: String = cose.iter().map(|c| format!("<div>{c}</div>")).collect();
    let corpo = format!(
        r#"<div>Prestazioni selezionate: {n}</div>{cose}<span>Quando:</span><span>{quando}</span><span>alle ore</span><span>09:00</span><div>{sede}</div><div>{amb}</div>{note}<input type="hidden" name="{RIEPILOGO}:privacy" value="si" /><div id="{RIEPILOGO}:riepilogo-nextButton-bottom">Conferma</div>"#,
        n = cose.matches("<div>").count()
    );
    format!("<h1>Riepilogo e conferma</h1>{}", modulo(RIEPILOGO, azione, &corpo))
}

/// La risposta a "Disdici": il dialogo di conferma con questi pulsanti.
pub fn dialogo(pulsanti: &[(&str, &str)]) -> String {
    let p: String = pulsanti
        .iter()
        .map(|(id, testo)| format!(r#"<div id="{L}:{id}"><button><span>{testo}</span></button></div>"#))
        .collect();
    format!(
        r#"<partial-response><changes><update id="x"><![CDATA[<div>Vuoi disdire?{p}</div>]]></update><update id="javax.faces.ViewState"><![CDATA[vs-dialogo]]></update></changes></partial-response>"#
    )
}

/// La pagina degli appuntamenti dopo "Sposta": la proposta e il pulsante "Altre disponibilita'".
pub fn appuntamenti(azione: &str, proposta: &str) -> String {
    let corpo = format!(
        r#"<div>'{A}:x:0:app_selector'</div>{proposta}<div class="btn btn-default" id="{A}:altre">Altre disponibilità</div>"#
    );
    format!("<h1>Appuntamenti Proposti</h1>{}", modulo(A, azione, &corpo))
}

/// Il pannello "Appuntamenti Disponibili" con queste date e, se `area`, il pulsante "Estendi area di ricerca".
pub fn disponibili(date: &[String], area: bool, avviso: bool) -> String {
    let estendi = if area { format!(r#"<div id="{A}:nextArea">Estendi area di ricerca</div>"#) } else { String::new() };
    let pulisci =
        if avviso { format!("<script>ice.s(event, this, '{A}:notifyCleaner');</script>") } else { String::new() };
    format!("<partial-response>Appuntamenti Disponibili{}{estendi}{pulisci}</partial-response>", date.concat())
}
