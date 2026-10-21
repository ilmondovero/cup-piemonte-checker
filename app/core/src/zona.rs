//! Dove e quando va bene una data nuova: zona di ricerca, sedi escluse, giorni da evitare.
//! Le regole sono quelle del bot Telegram (telegram-bot/cup_http.py), verificate sul portale.

use std::collections::HashMap;
use std::sync::LazyLock;

use chrono::{Datelike, NaiveDate};
use regex::Regex;
use unicode_normalization::UnicodeNormalization;

use crate::analisi::{norm, Luogo, Prenotazione, Slot};
use crate::errore::Errore;

/// Quante volte premere "Estendi area di ricerca": le aree lontane compaiono solo se hanno posti.
pub const ESTENDI_MAX: usize = 4;

/// Torino e i comuni confinanti della prima cintura.
pub const CINTURA_TORINO: [&str; 12] = [
    "TORINO",
    "BEINASCO",
    "BORGARO TORINESE",
    "COLLEGNO",
    "GRUGLIASCO",
    "MONCALIERI",
    "NICHELINO",
    "ORBASSANO",
    "RIVOLI",
    "SAN MAURO TORINESE",
    "SETTIMO TORINESE",
    "VENARIA REALE",
];

/// I nomi corti sono spesso anche parole ("SALE", "ALTO", "NONE"): nel nome di una sede valgono solo questi.
const COMUNI_CORTI: [&str; 6] = ["SUSA", "BRA", "ASTI", "ALBA", "CEVA", "GAVI"];

/// "Mondovì" e "MONDOVI'" -> "MONDOVI": il portale scrive gli accenti come apostrofi.
pub fn chiave_comune(nome: &str) -> String {
    norm(&nome.nfkd().filter(char::is_ascii).collect::<String>())
}

struct Comune {
    nome: String,
    provincia: String,
}

/// I comuni del Piemonte (ISTAT), per chiave: nome maiuscolo e sigla della provincia.
static COMUNI: LazyLock<HashMap<String, Comune>> = LazyLock::new(|| {
    include_str!("../data/comuni_piemonte.txt")
        .lines()
        .filter(|r| !r.is_empty() && !r.starts_with('#'))
        .filter_map(|r| {
            let mut c = r.split(';');
            let (nome, provincia) = (c.next()?, c.next()?);
            Some((chiave_comune(nome), Comune { nome: nome.to_uppercase(), provincia: provincia.into() }))
        })
        .collect()
});

static FINE_INDIRIZZO: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"([^()]*?)\s*\([A-Z]{2}\)\s*$").unwrap());
static PROVINCIA: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\(([A-Z]{2})\)\s*$").unwrap());
static TRATTINO: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s*-\s*").unwrap());

/// Il comune di una sede dall'indirizzo ("Via Roma, 1 - TORINO (TO)" -> "TORINO"). Senza provincia
/// nell'indirizzo, il comune scritto come pezzo intero del nome della sede ("PRESIDIO - SUSA"). "" se non si legge.
pub fn comune(luogo: &Luogo) -> String {
    let indirizzo = luogo.indirizzo.to_uppercase();
    let pezzi: Vec<&str> = FINE_INDIRIZZO
        .captures(&indirizzo)
        .map(|c| c.get(1).unwrap().as_str().split('-').map(str::trim).collect())
        .unwrap_or_default();
    if pezzi.len() >= 3 {
        let doppio = pezzi[pezzi.len() - 2..].join("-");
        if COMUNI.contains_key(&chiave_comune(&doppio)) {
            return doppio; // comune col trattino nel nome: "... - BEURA-CARDEZZA (VB)"
        }
    }
    if pezzi.len() >= 2 {
        return pezzi[pezzi.len() - 1].to_string();
    }
    let sede = luogo.sede.to_uppercase();
    TRATTINO
        .split(&sede)
        .find(|pezzo| {
            let k = chiave_comune(pezzo);
            COMUNI.contains_key(&k) && (k.len() > 4 || COMUNI_CORTI.contains(&k.as_str()))
        })
        .map(|p| p.trim().to_string())
        .unwrap_or_default()
}

/// La sigla della provincia dall'indirizzo ("... - TORINO (TO)" -> "TO"); se manca, quella del comune.
pub fn provincia(luogo: &Luogo) -> String {
    if let Some(c) = PROVINCIA.captures(&luogo.indirizzo.to_uppercase()) {
        return c[1].to_string();
    }
    COMUNI.get(&chiave_comune(&comune(luogo))).map(|c| c.provincia.clone()).unwrap_or_default()
}

/// Il nome ISTAT maiuscolo di un comune scritto dall'utente ("mondovi" -> "MONDOVÌ"), se e' in Piemonte.
pub fn nome_comune(nome: &str) -> Option<String> {
    COMUNI.get(&chiave_comune(nome)).map(|c| c.nome.clone())
}

/// Una sede scelta o esclusa: la coppia sede + comune (ci sono sedi omonime in comuni diversi).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Sede {
    pub sede: String,
    pub comune: String,
}

impl Sede {
    fn chiave(&self) -> (String, String) {
        (norm(&self.sede), chiave_comune(&self.comune))
    }

    fn di(luogo: &Luogo) -> (String, String) {
        (norm(&luogo.sede), chiave_comune(&comune(luogo)))
    }
}

/// Dove l'utente accetta una data nuova. Un valore vuoto (sede, comune, provincia) vuol dire "quello della
/// prenotazione attuale".
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Dove {
    Sede(String),
    Sedi(Vec<Sede>),
    Comune(String),
    Comuni(Vec<String>),
    Provincia(String),
    Tutte,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Zona {
    pub dove: Dove,
    /// Le sedi che l'utente non vuole, qualunque sia la zona.
    pub escluse: Vec<Sede>,
}

impl Zona {
    pub fn new(dove: Dove) -> Self {
        Zona { dove, escluse: vec![] }
    }

    /// Per sedi, comune, comuni e provincia serve estendere l'area: le sedi fuori dall'azienda della
    /// prenotazione compaiono solo cosi'. "sede" e "tutte" restano nell'area proposta dal CUP.
    pub fn estensioni(&self) -> usize {
        match self.dove {
            Dove::Sede(_) | Dove::Tutte => 0,
            _ => ESTENDI_MAX,
        }
    }

    /// La data e' in un posto che va bene? Se il dato che serve al confronto non si legge, no. Senza
    /// prenotazione la zona deve avere il suo valore: non c'e' una sede da cui ricavarlo.
    pub fn ammette(&self, slot: &Slot, attuale: Option<&Prenotazione>) -> bool {
        let l = &slot.luogo;
        if l.sede.is_empty() || self.escluse.iter().any(|x| x.chiave() == Sede::di(l)) {
            return false; // luogo non leggibile, o una sede che l'utente non vuole
        }
        let o = |v: &String, da: fn(&Luogo) -> String| {
            if v.is_empty() {
                attuale.map(|a| da(&a.luogo)).unwrap_or_default()
            } else {
                v.clone()
            }
        };
        match &self.dove {
            Dove::Sede(v) => {
                let rif = o(v, |l| l.sede.clone());
                !rif.is_empty() && norm(&l.sede) == norm(&rif)
            }
            Dove::Sedi(scelte) => scelte.iter().any(|x| x.chiave() == Sede::di(l)),
            Dove::Comune(v) => {
                let rif = o(v, comune);
                !rif.is_empty() && chiave_comune(&comune(l)) == chiave_comune(&rif)
            }
            Dove::Comuni(nomi) => {
                let k = chiave_comune(&comune(l));
                !k.is_empty() && nomi.iter().any(|x| chiave_comune(x) == k)
            }
            Dove::Provincia(v) => {
                let rif = o(v, provincia);
                !rif.is_empty() && provincia(l) == rif.to_uppercase()
            }
            Dove::Tutte => true,
        }
    }
}

/// I giorni in cui l'utente non puo' andare. Vuoto: tutti i giorni vanno bene.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Calendario {
    /// Giorni singoli.
    pub no: Vec<NaiveDate>,
    /// Giorni della settimana, lunedi' = 0.
    pub no_settimana: Vec<u32>,
    /// Fino a questo giorno compreso, tutti no.
    pub no_fino: Option<NaiveDate>,
    /// Dopo questo giorno, tutti no.
    pub si_fino: Option<NaiveDate>,
}

impl Calendario {
    pub fn giorno_si(&self, d: NaiveDate) -> bool {
        if self.no_fino.is_some_and(|f| d <= f) || self.si_fino.is_some_and(|f| d > f) {
            return false;
        }
        !self.no_settimana.contains(&d.weekday().num_days_from_monday()) && !self.no.contains(&d)
    }
}

/// Perche' una data non va bene, o None se va bene. La regola unica delle date da proporre: prenotabile, nella
/// zona, in un giorno si' e prima della prenotazione. Se la prenotazione e' in un giorno no va bene anche una data
/// piu' tardi (dopo lo spostamento e' in un giorno si': niente spostamenti a catena). Senza prenotazione ogni
/// giorno si' va bene.
pub fn perche_no(slot: &Slot, attuale: Option<&Prenotazione>, zona: &Zona, cal: &Calendario) -> Option<&'static str> {
    if !(slot.proposta || slot.seleziona.is_some()) {
        return Some("non si può scegliere");
    }
    if !zona.ammette(slot, attuale) {
        return Some("fuori zona");
    }
    if !cal.giorno_si(slot.quando.date()) {
        return Some("giorno da evitare");
    }
    let dopo = attuale.is_some_and(|a| slot.quando >= a.quando && cal.giorno_si(a.quando.date()));
    dopo.then_some("non è prima della prenotazione")
}

pub fn candidata(slot: &Slot, attuale: Option<&Prenotazione>, zona: &Zona, cal: &Calendario) -> bool {
    perche_no(slot, attuale, zona, cal).is_none()
}

const PROVINCE: [&str; 8] = ["AL", "AT", "BI", "CN", "NO", "TO", "VB", "VC"];
const GIORNI: [&str; 7] = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"];

/// "ven", "venerdì", "Venerdi" -> 4 (lunedi' = 0).
pub fn giorno_settimana(nome: &str) -> Option<u32> {
    let k = chiave_comune(nome).to_lowercase();
    GIORNI.iter().position(|g| k.len() >= 3 && k.starts_with(g)).map(|i| i as u32)
}

impl Dove {
    /// La zona scelta dall'utente: "sede", "comune", "comuni" (nomi separati da virgole), "cintura" (Torino e
    /// prima cintura), "provincia" (sigla) o "tutte". Un valore vuoto e' quello della prenotazione.
    pub fn da_scelta(tipo: &str, valore: &str) -> Result<Dove, Errore> {
        let v = valore.trim();
        let comune = |n: &str| {
            nome_comune(n)
                .ok_or_else(|| Errore::DatiNonValidi(format!("Non trovo il comune \"{}\" in Piemonte", n.trim())))
        };
        match tipo {
            "sede" => Ok(Dove::Sede(v.into())),
            "comune" if v.is_empty() => Ok(Dove::Comune(String::new())),
            "comune" => comune(v).map(Dove::Comune),
            "comuni" => {
                let nomi = v.split(',').filter(|n| !n.trim().is_empty()).map(comune).collect::<Result<Vec<_>, _>>()?;
                if nomi.is_empty() {
                    return Err(Errore::DatiNonValidi("Scrivi almeno un comune".into()));
                }
                Ok(Dove::Comuni(nomi))
            }
            "cintura" => Ok(Dove::Comuni(CINTURA_TORINO.map(String::from).to_vec())),
            "provincia" if v.is_empty() || PROVINCE.contains(&v.to_uppercase().as_str()) => {
                Ok(Dove::Provincia(v.to_uppercase()))
            }
            "provincia" => Err(Errore::DatiNonValidi(format!("\"{v}\" non è una provincia del Piemonte"))),
            "tutte" => Ok(Dove::Tutte),
            _ => Err(Errore::DatiNonValidi(format!("Zona sconosciuta: {tipo}"))),
        }
    }

    /// "solo nel comune di Torino", "Torino e prima cintura", ...
    pub fn descrizione(&self, attuale: Option<&Prenotazione>) -> String {
        let titolo = |s: &str| {
            s.split(' ')
                .map(|p| {
                    let mut c = p.chars();
                    c.next().map_or(String::new(), |f| f.to_uppercase().chain(c.flat_map(char::to_lowercase)).collect())
                })
                .collect::<Vec<_>>()
                .join(" ")
        };
        let o = |v: &String, da: fn(&Luogo) -> String| {
            if v.is_empty() {
                attuale.map(|a| da(&a.luogo)).unwrap_or_default()
            } else {
                v.clone()
            }
        };
        // senza prenotazione letta (o se non si legge) si dice "della prenotazione"
        let quale = |v: String, prima: &str, dopo: &str| {
            if v.is_empty() {
                format!("{prima} della prenotazione")
            } else {
                format!("{dopo} {v}")
            }
        };
        match self {
            Dove::Sede(v) => quale(o(v, |l| l.sede.clone()), "solo nella sede", "solo nella sede"),
            Dove::Sedi(s) => format!("in {} sedi scelte", s.len()),
            Dove::Comune(v) => quale(titolo(&o(v, comune)), "solo nel comune", "solo nel comune di"),
            Dove::Comuni(c)
                if c.iter().map(|x| chiave_comune(x)).eq(CINTURA_TORINO.iter().map(|x| chiave_comune(x))) =>
            {
                "a Torino e nella prima cintura".into()
            }
            Dove::Comuni(c) => format!("nei comuni di {}", c.iter().map(|x| titolo(x)).collect::<Vec<_>>().join(", ")),
            Dove::Provincia(v) => quale(o(v, provincia), "solo nella provincia", "solo in provincia di"),
            Dove::Tutte => "in tutte le sedi proposte dal CUP".into(),
        }
    }
}

#[cfg(test)]
mod test {
    use super::*;

    fn luogo(sede: &str, indirizzo: &str) -> Luogo {
        Luogo { sede: sede.into(), ambulatorio: "AMB".into(), indirizzo: indirizzo.into() }
    }

    fn giorno(a: i32, m: u32, g: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(a, m, g).unwrap()
    }

    fn slot(d: NaiveDate, l: Luogo) -> Slot {
        Slot { quando: d.and_hms_opt(10, 0, 0).unwrap(), luogo: l, seleziona: Some("s1".into()), proposta: false }
    }

    #[test]
    fn comuni_del_piemonte() {
        assert_eq!(COMUNI.len(), 1180);
        assert_eq!(chiave_comune("Mondovì"), chiave_comune("MONDOVI'"));
        assert_eq!(nome_comune("mondovi").as_deref(), Some("MONDOVÌ"));
        assert_eq!(nome_comune("Parigi"), None);
    }

    #[test]
    fn comune_e_provincia_dall_indirizzo_o_dalla_sede() {
        // dal vivo (2026-09-28): Susa scartata da "provincia di Torino" perche' l'indirizzo era "CORSO INGHILTERRA - ()"
        let susa = luogo("PRESIDIO - SUSA", "CORSO INGHILTERRA - ()");
        assert_eq!((comune(&susa), provincia(&susa)), ("SUSA".into(), "TO".into()));
        assert_eq!(provincia(&luogo("POLIAMBULATORIO - SALE", "VIA X - ()")), ""); // parola, non il comune
        assert_eq!(provincia(&luogo("OSPEDALE - MONDOVI'", "")), "CN");
        assert_eq!(provincia(&luogo("OSPEDALE", "VIA ROMA 1 - MONDOVI' ()")), ""); // meglio non indovinare
        assert_eq!(provincia(&luogo("X", "Via Po 1 - TORINO (TO)")), "TO"); // l'indirizzo vale per primo
        let beura = luogo("OSPEDALE", "VIA ROMA 1 - BEURA-CARDEZZA (VB)");
        assert_eq!((comune(&beura), provincia(&beura)), ("BEURA-CARDEZZA".into(), "VB".into()));
        assert_eq!(comune(&luogo("X", "VIA A-B - MONCALIERI (TO)")), "MONCALIERI");
        assert_eq!(comune(&luogo("X", "Largo Turati 62 - TORINO (TO)")), "TORINO");
        assert_eq!(comune(&luogo("X", "- MONCALIERI (TO)")), "MONCALIERI"); // senza via, come le Molinette
        assert_eq!(comune(&luogo("OSP - RIVOLI", "VIA X (ANGOLO Y) - TORINO (TO)")), "TORINO");
    }

    #[test]
    fn zone() {
        let att = Prenotazione {
            quando: giorno(2027, 1, 7).and_hms_opt(11, 45, 0).unwrap(),
            luogo: luogo("MOLINETTE", "- TORINO (TO)"),
            cosa: "VISITA".into(),
        };
        let mauriziano = slot(giorno(2026, 12, 1), luogo("MAURIZIANO", "Largo Turati 62 - TORINO (TO)"));
        let moncalieri =
            slot(giorno(2026, 12, 1), luogo("SANTA CROCE", "Piazza Amedeo Ferdinando 3 - MONCALIERI (TO)"));
        let asti = slot(giorno(2026, 12, 1), luogo("CARDINAL MASSAIA", "Corso Dante 202 - ASTI (AT)"));
        let ammessi = |dove: Dove| {
            let z = Zona::new(dove);
            [&mauriziano, &moncalieri, &asti].map(|s| z.ammette(s, Some(&att)))
        };
        assert_eq!(ammessi(Dove::Comune(String::new())), [true, false, false]); // il comune della prenotazione
        assert_eq!(ammessi(Dove::Comune("Moncalieri".into())), [false, true, false]);
        assert_eq!(ammessi(Dove::Comuni(CINTURA_TORINO.map(String::from).to_vec())), [true, true, false]);
        assert_eq!(ammessi(Dove::Provincia(String::new())), [true, true, false]);
        assert_eq!(ammessi(Dove::Tutte), [true, true, true]);
        assert_eq!(ammessi(Dove::Sede(String::new())), [false, false, false]); // solo le Molinette
        let sedi = Dove::Sedi(vec![Sede { sede: "Santa Croce".into(), comune: "MONCALIERI".into() }]);
        assert_eq!(ammessi(sedi), [false, true, false]);
        let mut z = Zona::new(Dove::Tutte);
        z.escluse = vec![Sede { sede: "MAURIZIANO".into(), comune: "Torino".into() }];
        assert!(!z.ammette(&mauriziano, Some(&att)) && z.ammette(&asti, Some(&att)));
        // una sede omonima in un altro comune non e' quella esclusa
        z.escluse = vec![Sede { sede: "MAURIZIANO".into(), comune: "ASTI".into() }];
        assert!(z.ammette(&mauriziano, Some(&att)));
        // senza prenotazione la zona deve dire dove
        assert!(!Zona::new(Dove::Comune(String::new())).ammette(&mauriziano, None));
        assert_eq!((Zona::new(Dove::Tutte).estensioni(), Zona::new(Dove::Comune("".into())).estensioni()), (0, 4));
    }

    #[test]
    fn zona_scelta_dall_utente() {
        assert_eq!(Dove::da_scelta("comune", " mondovi ").unwrap(), Dove::Comune("MONDOVÌ".into()));
        assert_eq!(Dove::da_scelta("comune", "").unwrap(), Dove::Comune(String::new()));
        assert_eq!(
            Dove::da_scelta("comuni", "Torino, moncalieri,").unwrap(),
            Dove::Comuni(vec!["TORINO".into(), "MONCALIERI".into()])
        );
        assert!(
            matches!(Dove::da_scelta("comuni", "Torino, Parigi"), Err(Errore::DatiNonValidi(m)) if m.contains("Parigi"))
        );
        assert!(matches!(Dove::da_scelta("comuni", " , "), Err(Errore::DatiNonValidi(_))));
        assert_eq!(Dove::da_scelta("provincia", "cn").unwrap(), Dove::Provincia("CN".into()));
        assert!(Dove::da_scelta("provincia", "MI").is_err() && Dove::da_scelta("ovunque", "").is_err());
        let att = Prenotazione {
            quando: giorno(2027, 1, 7).and_hms_opt(11, 45, 0).unwrap(),
            luogo: luogo("MOLINETTE", "- TORINO (TO)"),
            cosa: "VISITA".into(),
        };
        let descrivi = |t, v| Dove::da_scelta(t, v).unwrap().descrizione(Some(&att));
        assert_eq!(descrivi("comune", ""), "solo nel comune di Torino");
        assert_eq!(descrivi("cintura", ""), "a Torino e nella prima cintura");
        assert_eq!(descrivi("comuni", "san mauro torinese,Asti"), "nei comuni di San Mauro Torinese, Asti");
        assert_eq!(descrivi("provincia", ""), "solo in provincia di TO");
        assert_eq!(descrivi("sede", ""), "solo nella sede MOLINETTE");
        let senza = |t| Dove::da_scelta(t, "").unwrap().descrizione(None);
        assert_eq!(senza("comune"), "solo nel comune della prenotazione");
        assert_eq!(senza("provincia"), "solo nella provincia della prenotazione");
        assert_eq!(senza("sede"), "solo nella sede della prenotazione");
        assert_eq!(Dove::da_scelta("comune", "asti").unwrap().descrizione(None), "solo nel comune di Asti");
        assert_eq!(giorno_settimana("Venerdì"), Some(4));
        assert_eq!(
            (giorno_settimana("ven"), giorno_settimana("lun"), giorno_settimana("do")),
            (Some(4), Some(0), None)
        );
    }

    #[test]
    fn giorni_da_evitare_e_date_candidate() {
        let att = Prenotazione {
            quando: giorno(2027, 1, 7).and_hms_opt(11, 45, 0).unwrap(), // un giovedi'
            luogo: luogo("MOLINETTE", "- TORINO (TO)"),
            cosa: "VISITA".into(),
        };
        let z = Zona::new(Dove::Comune(String::new()));
        let cal = Calendario { no_settimana: vec![4], ..Default::default() }; // niente venerdi'
        let a_torino = |d| slot(d, luogo("MAURIZIANO", "- TORINO (TO)"));
        assert!(candidata(&a_torino(giorno(2026, 12, 3)), Some(&att), &z, &cal)); // giovedi'
        assert!(!candidata(&a_torino(giorno(2026, 12, 4)), Some(&att), &z, &cal)); // venerdi'
        assert!(!candidata(&a_torino(giorno(2027, 2, 4)), Some(&att), &z, &cal)); // dopo la prenotazione
        let mut senza_seleziona = a_torino(giorno(2026, 12, 3));
        senza_seleziona.seleziona = None;
        assert!(!candidata(&senza_seleziona, Some(&att), &z, &cal)); // non si potrebbe scegliere
                                                                     // la prenotazione cade in un giorno no: va bene anche piu' tardi, in un giorno si'
        let giovedi_no = Calendario { no: vec![giorno(2027, 1, 7)], ..Default::default() };
        assert!(candidata(&a_torino(giorno(2027, 2, 4)), Some(&att), &z, &giovedi_no));
        assert_eq!(perche_no(&a_torino(giorno(2026, 12, 4)), Some(&att), &z, &cal), Some("giorno da evitare"));
        assert_eq!(
            perche_no(&a_torino(giorno(2027, 2, 4)), Some(&att), &z, &cal),
            Some("non è prima della prenotazione")
        );
        let ad_asti = slot(giorno(2026, 12, 3), luogo("OSPEDALE", "- ASTI (AT)"));
        assert_eq!(perche_no(&ad_asti, Some(&att), &z, &cal), Some("fuori zona"));
        let finestra = Calendario {
            no_fino: Some(giorno(2026, 12, 1)),
            si_fino: Some(giorno(2026, 12, 20)),
            ..Default::default()
        };
        assert!(!finestra.giorno_si(giorno(2026, 12, 1)) && finestra.giorno_si(giorno(2026, 12, 2)));
        assert!(finestra.giorno_si(giorno(2026, 12, 20)) && !finestra.giorno_si(giorno(2026, 12, 21)));
    }
}
