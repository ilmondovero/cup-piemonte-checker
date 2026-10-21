//! Lettura delle pagine del portale: testo, date, luoghi, prestazioni, moduli JSF ed elenco prenotazioni.
//! Le regole sono le stesse del bot Telegram (telegram-bot/cup_http.py), che le ha verificate sul portale.

use std::fmt;
use std::sync::LazyLock;

use chrono::{Datelike, NaiveDate, NaiveDateTime};
use regex::Regex;

use crate::errore::Errore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Luogo {
    pub sede: String,
    pub ambulatorio: String,
    pub indirizzo: String,
}

impl fmt::Display for Luogo {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // senza via il portale scrive "- TORINO (TO)": a chi legge il trattino iniziale non dice nulla
        let indirizzo = self.indirizzo.trim_start_matches(['-', ' ']);
        let parti: Vec<&str> =
            [self.sede.as_str(), self.ambulatorio.as_str(), indirizzo].into_iter().filter(|x| !x.is_empty()).collect();
        f.write_str(&parti.join(", "))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Prenotazione {
    pub quando: NaiveDateTime,
    pub luogo: Luogo,
    /// es. "PRIMA VISITA ... - 89.7"
    pub cosa: String,
}

/// Una data offerta da "Sposta appuntamento".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Slot {
    pub quando: NaiveDateTime,
    pub luogo: Luogo,
    /// Il pulsante "Seleziona" della data; la proposta non ne ha, e' gia' selezionata.
    pub seleziona: Option<String>,
    /// La data che il portale propone per prima ("Appuntamenti Proposti").
    pub proposta: bool,
}

impl Slot {
    /// Data e luogo: la stessa data letta in due controlli ha la stessa chiave.
    pub fn chiave(&self) -> String {
        format!(
            "{}|{}",
            self.quando.format("%Y%m%d%H%M"),
            norm(&format!("{}{}", self.luogo.sede, self.luogo.ambulatorio))
        )
    }
}

/// L'elenco "Recupera Prenotazioni" di una ricetta.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Elenco {
    /// La prima riga in stato PRENOTATO: quella che "Sposta" anticiperebbe.
    pub attuale: Prenotazione,
    /// Tutte le righe PRENOTATO con una data leggibile (una per prestazione, se piu' d'una).
    pub prenotate: Vec<Prenotazione>,
    /// Righe PRENOTATO, anche quelle di cui non si legge la data.
    pub n_prenotate: usize,
    /// La riga dell'elenco della prenotazione attuale: i suoi pulsanti ("Sposta", "Disdici") hanno il suo numero.
    pub riga: usize,
}

/// Il modulo di Recupera Prenotazioni.
pub const L: &str = "_listaprenotazioni_WAR_cupprenotazione_:prescrizioniForm";
/// Il modulo della pagina degli appuntamenti (la stessa per "Sposta" e per una prenotazione nuova).
pub const A: &str = "_ricettaelettronica_WAR_cupprenotazione_:appuntamentiForm";

const MESI: [&str; 12] = [
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
];
const GIORNI: [&str; 7] = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"];

macro_rules! re {
    ($nome:ident, $s:expr) => {
        static $nome: LazyLock<Regex> = LazyLock::new(|| Regex::new($s).unwrap());
    };
}

re!(SCRIPT, r"(?is)<script\b.*?</script>");
re!(STYLE, r"(?is)<style\b.*?</style>");
re!(TAG, r"<[^>]+>");
re!(
    DATA,
    r"(?i)(?:luned|marted|mercoled|gioved|venerd|sabato|domenica)\S*\s+(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s+(\d{4})\s*alle ore\s*(\d{1,2}):(\d{2})"
);
re!(RIGA_LUOGO, r"<div>\s*<span[^>]*>([^<]*)</span>\s*</div>");
re!(INDIRIZZO, r#"(?s)class="unita-address"[^>]*>(.*?)</div>"#);
re!(COSA, r#"(?s)captionAppointment-desc">(.*?)</div>"#);
re!(PRENOTABILE, r"\s*\(PRENOTABILE\)\s*");
re!(STATO, r"Stato:\s*(\S+)");
re!(NESSUNA, r"Non esistono[^<]*|Nessun record[^<]*");
re!(PROPOSTA, r"'_ricettaelettronica_WAR_cupprenotazione_:appuntamentiForm:[^']*:0:app_selector'");
re!(SELETTORE, r"'(_ricettaelettronica_WAR_cupprenotazione_:appuntamentiForm:[^']*:0:(?:app|indisp)_selector)'");
re!(
    PULSANTE,
    r#"<div class="[^"]*\bbtn\b[^"]*" id="(_ricettaelettronica_WAR_cupprenotazione_:appuntamentiForm:[^"]+)"[^>]*>"#
);
re!(ID_A, r#"id="(_ricettaelettronica_WAR_cupprenotazione_:appuntamentiForm:[^"]+)"[^>]*>"#);
re!(SELEZIONA, r"\A>\s*Seleziona\s*<");
re!(AREA, r#"id="(_ricettaelettronica_WAR_cupprenotazione_:appuntamentiForm:nextArea)""#);
re!(PULISCI, r"'(_ricettaelettronica_WAR_cupprenotazione_:appuntamentiForm:notifyCleaner)'");
re!(CF, r"^[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]$");
re!(NRE, r"^[0-9A-Z]{15}$");

/// I primi `n` caratteri di `s` (mai a meta' di un carattere).
fn primi(s: &str, n: usize) -> &str {
    s.char_indices().nth(n).map_or(s, |(i, _)| &s[..i])
}

/// Il testo di un pezzo di pagina: senza script, stili e tag, entita' decodificate, spazi compattati.
pub fn testo(frammento: &str) -> String {
    let s = SCRIPT.replace_all(frammento, " ");
    let s = STYLE.replace_all(&s, " ");
    let s = TAG.replace_all(&s, " ");
    html_escape::decode_html_entities(&s).split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Solo lettere e cifre maiuscole: per confrontare nomi di sedi scritti in modi diversi.
pub fn norm(s: &str) -> String {
    s.to_uppercase().chars().filter(|c| c.is_ascii_uppercase() || c.is_ascii_digit()).collect()
}

/// Il valore di un campo nascosto (`name="..." ... value="..."`).
pub fn nascosto(pagina: &str, nome: &str) -> Option<String> {
    let re = Regex::new(&format!(r#"name="{}"[^>]*value="([^"]*)""#, regex::escape(nome))).ok()?;
    re.captures(pagina).map(|c| html_escape::decode_html_entities(&c[1]).into_owned())
}

/// "Lunedì 1 Marzo 2027 alle ore 09:00" -> 2027-03-01 09:00. None se non c'e' una data valida.
pub fn data(testo: &str) -> Option<NaiveDateTime> {
    let c = DATA.captures(testo)?;
    let mese = MESI.iter().position(|m| m.eq_ignore_ascii_case(&c[2]))? as u32 + 1;
    NaiveDate::from_ymd_opt(c[3].parse().ok()?, mese, c[1].parse().ok()?)?.and_hms_opt(
        c[4].parse().ok()?,
        c[5].parse().ok()?,
        0,
    )
}

/// Blocco "Dove:" del portale: sede e ambulatorio sono `<div>` con un solo `<span>`, l'indirizzo sta nel div
/// "unita-address" (le righe di zona hanno piu' span e vengono saltate).
pub fn luogo(frammento: &str) -> Luogo {
    let seg = frammento.find("Dove:").map_or("", |i| primi(&frammento[i..], 3000));
    let righe: Vec<String> = RIGA_LUOGO.captures_iter(seg).map(|c| testo(&c[1])).filter(|x| !x.is_empty()).collect();
    Luogo {
        sede: righe.first().cloned().unwrap_or_default(),
        ambulatorio: righe.get(1).cloned().unwrap_or_default(),
        // com'e' scritto, anche "- TORINO (TO)" senza via: il comune si legge dai pezzi tra i trattini
        indirizzo: INDIRIZZO.captures(seg).map(|c| testo(&c[1])).unwrap_or_default(),
    }
}

/// La prestazione di un appuntamento, senza "(PRENOTABILE)".
pub fn cosa(frammento: &str) -> String {
    COSA.captures(frammento).map(|c| PRENOTABILE.replace_all(&testo(&c[1]), "").trim().to_string()).unwrap_or_default()
}

/// Il `<form>` con questo id, per intero.
pub fn modulo<'a>(pagina: &'a str, id: &str) -> Option<&'a str> {
    let re = Regex::new(&format!(r#"(?s)<form[^>]*id="{}".*?</form>"#, regex::escape(id))).ok()?;
    re.find(pagina).map(|m| m.as_str())
}

/// L'elenco delle prenotazioni della ricetta (risposta di "Cerca" in Recupera Prenotazioni).
pub fn elenco(xml: &str) -> Result<Elenco, Errore> {
    let inizi: Vec<usize> = xml.match_indices("Stato:").map(|(i, _)| i).collect();
    if inizi.is_empty() {
        // "nessuna prenotazione" solo se il portale lo dice: una pagina diversa (manutenzione, errore, sito
        // cambiato) non deve far credere che la ricetta sia libera da prenotare
        return Err(match NESSUNA.find(xml) {
            Some(m) => Errore::NonTrovata(m.as_str().trim().to_string()),
            None => {
                Errore::Inattesa("Elenco delle prenotazioni non leggibile (portale in manutenzione o cambiato?)".into())
            }
        });
    }
    let fini = inizi.iter().skip(1).copied().chain([xml.len()]);
    let righe: Vec<&str> = inizi.iter().zip(fini).map(|(&a, b)| &xml[a..b]).collect();
    let stati: Vec<String> =
        righe.iter().map(|r| STATO.captures(&testo(primi(r, 400))).map_or("?".into(), |c| c[1].to_string())).collect();
    let pren: Vec<usize> = (0..righe.len()).filter(|&i| stati[i].eq_ignore_ascii_case("PRENOTATO")).collect();
    let Some(&prima) = pren.first() else {
        return Err(Errore::NonAttiva { stati: stati.iter().map(|s| s.to_uppercase()).collect() });
    };
    let prenotate = pren
        .iter()
        .filter_map(|&i| {
            data(&testo(righe[i])).map(|quando| Prenotazione { quando, luogo: luogo(righe[i]), cosa: cosa(righe[i]) })
        })
        .collect();
    let quando = data(&testo(righe[prima]))
        .ok_or_else(|| Errore::Inattesa("Data dell'appuntamento attuale non leggibile".into()))?;
    Ok(Elenco {
        attuale: Prenotazione { quando, luogo: luogo(righe[prima]), cosa: cosa(righe[prima]) },
        prenotate,
        n_prenotate: pren.len(),
        riga: prima,
    })
}

/// Il pulsante della riga `riga` dell'elenco: "spostaButton", "disdiciButton".
pub fn pulsante_riga(xml: &str, riga: usize, nome: &str) -> Option<String> {
    let re = Regex::new(&format!(r#"id="({}:[^"]*:{riga}:{nome})""#, regex::escape(L))).ok()?;
    re.captures(xml).map(|c| c[1].to_string())
}

/// Il pezzo di `testo` dopo `inizio` (al piu' `n` caratteri) in cui puo' stare un'etichetta "dentro lo stesso
/// div" del pulsante: fino al primo "</div>", del quale resta solo il "<" (un'etichetta puo' finire li').
fn dentro_il_div(testo: &str, inizio: usize, n: usize) -> &str {
    let seg = primi(&testo[inizio..], n);
    seg.find("</div>").map_or(seg, |i| &seg[..i + 1])
}

/// La data proposta dal portale in cima alla pagina degli appuntamenti, se c'e'.
pub fn proposta(pagina: &str) -> Option<Slot> {
    let prop = pagina.find("Appuntamenti Proposti").map_or("", |i| &pagina[i..]);
    let quando = data(&testo(primi(prop, 6000)))?;
    PROPOSTA.is_match(pagina).then(|| Slot { quando, luogo: luogo(prop), seleziona: None, proposta: true })
}

/// (pulsante "Altre disponibilita'", selettore del pannello della proposta): il click su "Altre disponibilita'"
/// risale al pannello della proposta, e il browser invia prima il suo selettore (app_selector, o
/// indisp_selector se non c'e' nessuna proposta).
pub fn altre_disponibilita(pagina: &str) -> Option<(String, String)> {
    let altre = PULSANTE.captures_iter(pagina).find_map(|c| {
        let fine = c.get(0).unwrap().end();
        dentro_il_div(pagina, fine, 800 + 18).contains("Altre disponibilit").then(|| c[1].to_string())
    })?;
    Some((altre, SELETTORE.captures(pagina)?[1].to_string()))
}

/// Il pannello "Appuntamenti Disponibili" di una risposta, se c'e'.
pub fn disponibili(xml: &str) -> Option<&str> {
    if !testo(xml).contains("Appuntamenti Disponibili") {
        return None;
    }
    xml.find("Appuntamenti Disponibili").map(|i| &xml[i..])
}

/// Il pulsante "Estendi area di ricerca" del pannello.
pub fn estendi_area(disponibili: &str) -> Option<String> {
    AREA.captures(disponibili).map(|c| c[1].to_string())
}

/// Il comando che chiude l'avviso "area estesa".
pub fn chiudi_avviso(xml: &str) -> Option<String> {
    PULISCI.captures(xml).map(|c| c[1].to_string())
}

/// Le date del pannello "Appuntamenti Disponibili", con il loro pulsante "Seleziona". La proposta ripetuta in
/// cima all'elenco (stessa data, senza "Seleziona") non si conta due volte.
pub fn date_disponibili(disponibili: &str, proposta: Option<&Slot>) -> Vec<Slot> {
    let inizi: Vec<usize> =
        disponibili.match_indices(r#"<div class="captionAppointment-what""#).map(|(i, _)| i).collect();
    let fini = inizi.iter().skip(1).copied().chain([disponibili.len()]);
    let mut slots = vec![];
    for (a, b) in inizi.iter().copied().zip(fini) {
        let blocco = &disponibili[a..b];
        let Some(quando) = data(&testo(blocco)) else { continue };
        let seleziona = ID_A.captures_iter(blocco).find_map(|c| {
            let dopo = &blocco[c.get(0).unwrap().end()..];
            // come il bot: il ">" dell'etichetta entro 600 caratteri, senza passare un "</div>"
            let fino = dentro_il_div(dopo, 0, 600).len().min(dopo.find("</div>").unwrap_or(usize::MAX));
            dopo.char_indices()
                .take_while(|(i, _)| *i <= fino)
                .any(|(i, ch)| ch == '>' && SELEZIONA.is_match(&dopo[i..]))
                .then(|| c[1].to_string())
        });
        if proposta.is_some_and(|p| p.quando == quando) && seleziona.is_none() {
            continue;
        }
        slots.push(Slot { quando, luogo: luogo(blocco), seleziona, proposta: false });
    }
    slots
}

/// (da tenere, da disdire) se la stessa prestazione e' prenotata in due appuntamenti diversi (data o luogo): si
/// tiene il piu' vicino. None se non c'e' un doppione. Le righe gia' passate non contano: non si disdice mai
/// l'appuntamento futuro per tenerne uno trascorso.
pub fn doppione(prenotate: &[Prenotazione], adesso: NaiveDateTime) -> Option<(Prenotazione, Prenotazione)> {
    let mut gruppi: Vec<(String, Vec<&Prenotazione>)> = vec![];
    for x in prenotate.iter().filter(|x| !norm(&x.cosa).is_empty() && x.quando >= adesso) {
        match gruppi.iter_mut().find(|(k, _)| *k == norm(&x.cosa)) {
            Some((_, v)) => v.push(x),
            None => gruppi.push((norm(&x.cosa), vec![x])),
        }
    }
    let chiave = |x: &Prenotazione| (x.quando, norm(&format!("{}{}", x.luogo.sede, x.luogo.ambulatorio)));
    gruppi
        .into_iter()
        .filter_map(|(_, mut righe)| {
            righe.sort_by_key(|x| x.quando);
            let diverse = righe.iter().map(|x| chiave(x)).collect::<std::collections::HashSet<_>>().len() > 1;
            diverse.then(|| (righe[0].clone(), righe[righe.len() - 1].clone()))
        })
        .max_by_key(|(_, lontana)| lontana.quando)
}

/// Codice fiscale e numero ricetta come li vuole il portale (maiuscoli, senza spazi), o perche' non vanno.
pub fn credenziali(cf: &str, nre: &str) -> Result<(String, String), Errore> {
    let pulisci = |s: &str| s.chars().filter(|c| !c.is_whitespace()).collect::<String>().to_uppercase();
    let (cf, nre) = (pulisci(cf), pulisci(nre));
    if !CF.is_match(&cf) {
        return Err(Errore::DatiNonValidi("Il codice fiscale deve avere 16 caratteri, come RSSMRA80A01L219X".into()));
    }
    if !NRE.is_match(&nre) {
        return Err(Errore::DatiNonValidi(
            "Il numero della ricetta deve avere 15 caratteri, come 010A0 0000000001".into(),
        ));
    }
    Ok((cf, nre))
}

/// "gio 07/01/2027 ore 11:45"
pub fn breve(quando: &NaiveDateTime) -> String {
    format!("{} {}", GIORNI[quando.weekday().num_days_from_monday() as usize], quando.format("%d/%m/%Y ore %H:%M"))
}

#[cfg(test)]
mod test {
    use super::*;

    /// Un appuntamento come lo disegna il portale (lo stesso markup dei test del bot).
    fn blocco(quando: &str, sede: &str, amb: &str, zona: bool) -> String {
        let zone = if zona {
            r#"<div><span id="z1">AZIENDA ESEMPIO [MACRO-ZONA]</span><span id="z2"> - </span><span id="z3">AZIENDA ESEMPIO [ZONA]</span> </div>"#
        } else {
            "<div> </div>"
        };
        format!(
            r#"<div class="captionAppointment-what"><span>Cosa:</span> </div><div class="captionAppointment-desc"><span id="d">VISITA DI ESEMPIO - 11.11</span><span> (PRENOTABILE)</span> <br /> </div><span>Quando:</span><span class="fw-b">{quando}</span><span>alle ore</span><span class="fw-b">09:00</span><span>Dove: </span><br /><span>Mappa</span> </div> <div style="flex-grow:1;"> {zone}<div><span id="s">{sede}</span> </div> <div><span id="a">{amb}</span> </div> <div class="unita-address" data-address="Via Roma, 1"><span>Via Roma, 1</span><span> - TORINO (TO)</span> </div></div>"#
        )
    }

    fn ora(a: i32, m: u32, g: u32, h: u32, min: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(a, m, g).unwrap().and_hms_opt(h, min, 0).unwrap()
    }

    #[test]
    fn luogo_data_cosa() {
        let html =
            format!("<span>Stato:</span> PRENOTATO {}", blocco("Lunedì 1 Marzo 2027", "OSPEDALE A", "AMB 1", true));
        assert_eq!(data(&testo(&html)), Some(ora(2027, 3, 1, 9, 0)));
        assert_eq!(
            luogo(&html),
            Luogo {
                sede: "OSPEDALE A".into(),
                ambulatorio: "AMB 1".into(),
                indirizzo: "Via Roma, 1 - TORINO (TO)".into()
            }
        );
        assert_eq!(cosa(&html), "VISITA DI ESEMPIO - 11.11");
        assert_eq!(luogo(&html).to_string(), "OSPEDALE A, AMB 1, Via Roma, 1 - TORINO (TO)");
        let senza_via = html.replace("<span>Via Roma, 1</span>", "");
        assert_eq!(luogo(&senza_via).indirizzo, "- TORINO (TO)"); // grezzo: serve a leggere il comune
        assert_eq!(luogo(&senza_via).to_string(), "OSPEDALE A, AMB 1, TORINO (TO)");
    }

    #[test]
    fn date_impossibili_o_mesi_sconosciuti() {
        assert_eq!(data("Lunedì 1 Brumaio 2027 alle ore 09:00"), None);
        assert_eq!(data("Lunedì 31 Febbraio 2027 alle ore 09:00"), None);
        assert_eq!(data("gioved\u{ec} 7 gennaio 2027 alle ore 11:45"), Some(ora(2027, 1, 7, 11, 45)));
    }

    #[test]
    fn testo_senza_tag_script_ed_entita() {
        assert_eq!(testo("<b>Ciao</b><script>x()</script>  <i>Dar&igrave;o</i>&nbsp;!"), "Ciao Darìo !");
        assert_eq!(norm("Osp. Maria-Vittoria è"), "OSPMARIAVITTORIA");
    }

    #[test]
    fn campi_nascosti_e_moduli() {
        let p = r#"<form id="F"><input type="hidden" name="javax.faces.ViewState" value="a&amp;b" /></form><form id="G"></form>"#;
        assert_eq!(nascosto(p, "javax.faces.ViewState").as_deref(), Some("a&b"));
        assert_eq!(nascosto(p, "altro"), None);
        assert!(modulo(p, "F").unwrap().contains("ViewState") && !modulo(p, "F").unwrap().contains("\"G\""));
        assert_eq!(modulo(p, "H"), None);
    }

    #[test]
    fn elenco_prima_prenotata_e_righe_non_attive() {
        let xml = format!(
            "<span>Stato:</span> DISDETTO {}<span>Stato:</span> PRENOTATO {}<span>Stato:</span> PRENOTATO senza data",
            blocco("Martedì 2 Febbraio 2027", "OSPEDALE B", "AMB 2", false),
            blocco("Lunedì 1 Marzo 2027", "OSPEDALE A", "AMB 1", true),
        );
        let e = elenco(&xml).unwrap();
        assert_eq!(e.attuale.quando, ora(2027, 3, 1, 9, 0));
        assert_eq!(e.attuale.luogo.sede, "OSPEDALE A");
        assert_eq!((e.prenotate.len(), e.n_prenotate), (1, 2));
    }

    #[test]
    fn elenco_vuoto_disdetto_o_illeggibile() {
        assert!(
            matches!(elenco("<p>Nessun record trovato</p>"), Err(Errore::NonTrovata(m)) if m == "Nessun record trovato")
        );
        let disdetta = elenco("<span>Stato:</span> Disdetto <span>Stato:</span> ANNULLATO").unwrap_err();
        assert!(matches!(&disdetta, Errore::NonAttiva { stati } if stati == &["DISDETTO", "ANNULLATO"]));
        assert!(disdetta.solo_disdette());
        assert!(!elenco("<span>Stato:</span> EROGATO").unwrap_err().solo_disdette());
        // una pagina di manutenzione non e' "nessuna prenotazione"
        assert!(matches!(elenco("<html>Manutenzione</html>"), Err(Errore::Inattesa(_))));
        assert!(matches!(elenco("<span>Stato:</span> PRENOTATO senza data"), Err(Errore::Inattesa(_))));
    }

    #[test]
    fn credenziali_pulite_o_rifiutate() {
        assert_eq!(
            credenziali(" rssmra80a01l219x ", "010a0 0000000001").unwrap(),
            ("RSSMRA80A01L219X".into(), "010A00000000001".into())
        );
        assert!(matches!(credenziali("RSSMRA80A01L219", "010A00000000001"), Err(Errore::DatiNonValidi(_))));
        assert!(matches!(credenziali("RSSMRA80A01L219X", "010A0000000000"), Err(Errore::DatiNonValidi(_))));
    }

    #[test]
    fn proposta_altre_disponibilita_e_date() {
        use crate::prova::{appuntamenti, appuntamento, disponibili as pannello};
        let pagina = appuntamenti(
            "x",
            &appuntamento("Martedì 14 Settembre 2027", "POLIAMBULATORIO NORD", "ECO 1", "TORINO (TO)", None),
        );
        let p = proposta(&pagina).unwrap();
        assert!(p.proposta && p.seleziona.is_none() && p.luogo.sede == "POLIAMBULATORIO NORD");
        assert_eq!(p.quando, ora(2027, 9, 14, 9, 0));
        assert_eq!(altre_disponibilita(&pagina), Some((format!("{A}:altre"), format!("{A}:x:0:app_selector"))));
        // senza il selettore della proposta non c'e' proposta (la data in pagina e' di altro)
        assert_eq!(proposta(&pagina.replace("app_selector", "altro")), None);
        let xml = pannello(
            &[
                appuntamento("Martedì 14 Settembre 2027", "POLIAMBULATORIO NORD", "ECO 1", "TORINO (TO)", None),
                appuntamento(
                    "Lunedì 6 Settembre 2027",
                    "POLIAMBULATORIO SUD",
                    "ECO 2",
                    "TORINO (TO)",
                    Some(&format!("{A}:s1")),
                ),
                appuntamento("Venerdì 3 Settembre 2027", "OSPEDALE", "ECO 3", "ASTI (AT)", Some(&format!("{A}:s2"))),
            ],
            true,
            true,
        );
        let disp = disponibili(&xml).unwrap();
        let date = date_disponibili(disp, Some(&p));
        // la proposta ripetuta in cima all'elenco non si conta due volte
        assert_eq!(date.iter().map(|s| s.luogo.sede.as_str()).collect::<Vec<_>>(), ["POLIAMBULATORIO SUD", "OSPEDALE"]);
        assert_eq!(date[0].seleziona.as_deref(), Some(format!("{A}:s1").as_str()));
        assert!(!date[0].proposta && date[1].luogo.indirizzo == "Via Roma, 1 - ASTI (AT)");
        assert_eq!(estendi_area(disp), Some(format!("{A}:nextArea")));
        assert_eq!(chiudi_avviso(&xml), Some(format!("{A}:notifyCleaner")));
        assert_eq!(disponibili("<partial-response>niente</partial-response>"), None);
        // un "Seleziona" fuori dal div del pulsante non e' il suo
        let fuori = appuntamento("Lunedì 6 Settembre 2027", "SEDE", "AMB", "TORINO (TO)", None)
            + &format!(r#"<div id="{A}:s9"></div><span>Seleziona</span>"#);
        assert_eq!(date_disponibili(&fuori, None)[0].seleziona, None);
    }

    #[test]
    fn pulsanti_della_riga_dell_elenco() {
        let xml = crate::prova::elenco_prenotato("Lunedì 1 Marzo 2027", "OSPEDALE A", "TORINO (TO)");
        assert_eq!(elenco(&xml).unwrap().riga, 0);
        assert_eq!(pulsante_riga(&xml, 0, "spostaButton"), Some(format!("{L}:righe:0:spostaButton")));
        assert_eq!(pulsante_riga(&xml, 1, "spostaButton"), None);
    }

    #[test]
    fn seleziona_entro_600_caratteri_come_il_bot() {
        let blocco = |prima: usize, spazi: usize| {
            appuntamento_base()
                + &format!(
                    r#"<div id="{A}:s1"><span>{}>{}Seleziona</span></div>"#,
                    "x".repeat(prima),
                    " ".repeat(spazi)
                )
        };
        let trovato = |b: String| date_disponibili(&b, None)[0].seleziona.is_some();
        // il ">" prima dell'etichetta e' a 6 + `prima` caratteri dal pulsante
        assert!(trovato(blocco(0, 0)) && trovato(blocco(594, 0)));
        assert!(!trovato(blocco(595, 0))); // oltre i 600 caratteri
        assert!(trovato(blocco(580, 100))); // tanti spazi attorno all'etichetta: va bene, come nel bot
    }

    fn appuntamento_base() -> String {
        crate::prova::appuntamento("Lunedì 6 Settembre 2027", "SEDE", "AMB", "TORINO (TO)", None)
    }

    #[test]
    fn doppione_della_stessa_prestazione() {
        let p = |g: u32, sede: &str, cosa: &str| Prenotazione {
            quando: ora(2027, 1, g, 9, 0),
            luogo: Luogo { sede: sede.into(), ambulatorio: "AMB".into(), indirizzo: String::new() },
            cosa: cosa.into(),
        };
        let adesso = ora(2026, 12, 1, 0, 0);
        let (tenere, disdire) =
            doppione(&[p(20, "B", "VISITA - 1"), p(7, "A", "VISITA - 1"), p(9, "C", "ESAME - 2")], adesso).unwrap();
        assert_eq!((tenere.quando.day(), disdire.quando.day()), (7, 20));
        // prestazioni diverse, o la stessa ripetuta nello stesso appuntamento: nessun doppione
        assert_eq!(doppione(&[p(7, "A", "VISITA - 1"), p(9, "A", "ESAME - 2")], adesso), None);
        assert_eq!(doppione(&[p(7, "A", "VISITA - 1"), p(7, "A", "VISITA - 1")], adesso), None);
        // una riga gia' passata non conta
        assert_eq!(doppione(&[p(7, "A", "VISITA - 1"), p(20, "B", "VISITA - 1")], ora(2027, 1, 8, 0, 0)), None);
    }

    #[test]
    fn data_breve() {
        assert_eq!(breve(&ora(2027, 1, 7, 11, 45)), "gio 07/01/2027 ore 11:45");
    }
}
