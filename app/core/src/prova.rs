//! Pagine finte del portale per i test, con lo stesso markup dei test del bot (telegram-bot/tests).

use crate::analisi::{A, L};

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
