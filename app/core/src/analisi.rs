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
        let parti: Vec<&str> = [&self.sede, &self.ambulatorio, &self.indirizzo]
            .into_iter()
            .map(String::as_str)
            .filter(|x| !x.is_empty())
            .collect();
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

/// L'elenco "Recupera Prenotazioni" di una ricetta.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Elenco {
    /// La prima riga in stato PRENOTATO: quella che "Sposta" anticiperebbe.
    pub attuale: Prenotazione,
    /// Tutte le righe PRENOTATO con una data leggibile (una per prestazione, se piu' d'una).
    pub prenotate: Vec<Prenotazione>,
    /// Righe PRENOTATO, anche quelle di cui non si legge la data.
    pub n_prenotate: usize,
}

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
        // senza via il portale scrive "- TORINO (TO)": il trattino iniziale non dice nulla
        indirizzo: INDIRIZZO
            .captures(seg)
            .map(|c| testo(&c[1]).trim_start_matches(['-', ' ']).to_string())
            .unwrap_or_default(),
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
    })
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
        assert_eq!(luogo(&senza_via).indirizzo, "TORINO (TO)");
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
    fn data_breve() {
        assert_eq!(breve(&ora(2027, 1, 7, 11, 45)), "gio 07/01/2027 ore 11:45");
    }
}
