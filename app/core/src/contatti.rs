//! I contatti facoltativi di una ricetta: email e cellulare. Il CUP li usa per la conferma, il promemoria e gli SMS.
//! Vanno solo al portale, nel Riepilogo (vedi `portale::Sessione::conferma`): mai nel registro.

/// Email e cellulare gia' controllati (vedi `email` e `cellulare`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Contatti {
    pub email: Option<String>,
    /// Solo cifre, senza prefisso: "3331234567".
    pub cellulare: Option<String>,
}

impl Contatti {
    pub fn vuoti(&self) -> bool {
        self.email.is_none() && self.cellulare.is_none()
    }
}

/// Un'email scritta dall'utente: vuota = nessuna. In minuscolo, al massimo 100 caratteri, senza spazi, una "@", un
/// nome senza punti all'inizio, alla fine o doppi, e un dominio con il punto (nessuna parte vuota o che comincia o
/// finisce con "-"). Err con il messaggio per l'utente.
pub fn email(s: &str) -> Result<Option<String>, String> {
    let e = s.trim().to_lowercase();
    if e.is_empty() {
        return Ok(None);
    }
    let no = || Err("L'email non sembra giusta: scrivila come nome@esempio.it.".to_string());
    if e.chars().count() > 100 {
        return Err("L'email è troppo lunga (al massimo 100 caratteri).".into());
    }
    if e.chars().any(char::is_whitespace) {
        return no();
    }
    let Some((nome, dominio)) = e.split_once('@') else { return no() };
    let nome_ok = !nome.is_empty()
        && nome.chars().all(|c| c.is_ascii_alphanumeric() || "._%+-".contains(c))
        && !nome.starts_with('.')
        && !nome.ends_with('.')
        && !nome.contains("..");
    let parti: Vec<&str> = dominio.split('.').collect();
    let parte_ok = |p: &&str| {
        !p.is_empty()
            && !p.starts_with('-')
            && !p.ends_with('-')
            && p.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
    };
    let dominio_ok = parti.len() >= 2
        && parti.iter().all(parte_ok)
        && parti.last().is_some_and(|t| t.len() >= 2 && t.chars().all(|c| c.is_ascii_alphabetic()));
    if !nome_ok || !dominio_ok {
        return no();
    }
    Ok(Some(e))
}

/// Un cellulare italiano scritto dall'utente: vuoto = nessuno. Senza spazi, punti, trattini, parentesi e prefisso
/// +39/0039; deve cominciare con 3 e avere 9 o 10 cifre. Si controlla tutto quello che l'utente ha scritto (al
/// massimo 20 caratteri), senza tagliare nulla prima. Err con il messaggio per l'utente.
pub fn cellulare(s: &str) -> Result<Option<String>, String> {
    if s.trim().chars().count() > 20 {
        return Err("Il cellulare è troppo lungo: scrivi solo il numero, per esempio 333 123 4567.".into());
    }
    let c: String = s.chars().filter(|c| !" .-()/".contains(*c)).collect();
    if c.is_empty() {
        return Ok(None);
    }
    let c = c.strip_prefix("+39").or_else(|| c.strip_prefix("0039")).unwrap_or(&c);
    if !(9..=10).contains(&c.len()) || !c.starts_with('3') || !c.chars().all(|x| x.is_ascii_digit()) {
        return Err("Il cellulare non sembra giusto: serve un numero italiano che comincia con 3, per esempio \
                    333 123 4567."
            .into());
    }
    Ok(Some(c.to_string()))
}

/// "m***@gmail.com" (con un nome di una lettera sola "*@gmail.com"): nell'interfaccia l'email non si vede per intero.
pub fn email_mascherata(e: &str) -> String {
    match e.split_once('@') {
        Some((nome, dominio)) if nome.chars().count() > 1 => {
            format!("{}***@{dominio}", nome.chars().next().unwrap_or('*'))
        }
        Some((_, dominio)) => format!("*@{dominio}"),
        None => "***".into(),
    }
}

/// "*** *** 4567": nell'interfaccia del cellulare si vedono solo le ultime 4 cifre.
pub fn cellulare_mascherato(c: &str) -> String {
    if c.len() < 7 || !c.is_ascii() {
        return "***".into();
    }
    format!("*** *** {}", &c[c.len() - 4..])
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn email_controllata_e_in_minuscolo() {
        assert_eq!(email("  Mario.Rossi@Gmail.com "), Ok(Some("mario.rossi@gmail.com".into())));
        assert_eq!(email("a+b_c@poste-it.example.it"), Ok(Some("a+b_c@poste-it.example.it".into())));
        assert_eq!(email(""), Ok(None));
        assert_eq!(email("   "), Ok(None));
        for male in [
            "mario",
            "mario@",
            "@gmail.com",
            "mario@gmail",
            "mario@gmail.c",
            "ma rio@gmail.com",
            "mario@@gmail.com",
            "mario@gmail..com",
            ".mario@gmail.com",
            "mario@gmail.c0m",
            "mario@g_mail.com",
            "mario@gmail.com x",
            "mario\t@gmail.com",
            "mario.@gmail.com",
            "ma..rio@gmail.com",
            "mario@-gmail.com",
            "mario@gmail-.com",
        ] {
            assert!(email(male).is_err(), "{male}");
        }
        assert!(email(&format!("{}@gmail.com", "a".repeat(95))).unwrap_err().contains("100"));
    }

    #[test]
    fn cellulare_normalizzato() {
        for (scritto, atteso) in [
            ("333 123 4567", "3331234567"),
            ("+39 333.123.4567", "3331234567"),
            ("0039-333-1234567", "3331234567"),
            ("(333) 1234567", "3331234567"),
            ("333/123456", "333123456"),
        ] {
            assert_eq!(cellulare(scritto), Ok(Some(atteso.into())), "{scritto}");
        }
        assert_eq!(cellulare(" "), Ok(None));
        for male in ["011 1234567", "33312345", "33312345678", "333 12a 4567", "+44 7700 900123", "+39"] {
            assert!(cellulare(male).is_err(), "{male}");
        }
        // tutto quello che e' scritto, senza tagliare: 20 caratteri al massimo, e niente cifre in piu' dopo
        assert_eq!(cellulare("+39 (333) 123-45-67 "), Ok(Some("3331234567".into())));
        assert!(cellulare("+39 333 123 4567 int. 2").unwrap_err().contains("troppo lungo"));
        assert!(cellulare("333 123 4567 8").is_err());
    }

    #[test]
    fn mascherati() {
        assert_eq!(email_mascherata("mario.rossi@gmail.com"), "m***@gmail.com");
        assert_eq!(email_mascherata("m@gmail.com"), "*@gmail.com");
        assert_eq!(cellulare_mascherato("3331234567"), "*** *** 4567");
        assert_eq!(cellulare_mascherato("333123456"), "*** *** 3456");
        assert_eq!(cellulare_mascherato("12"), "***");
        assert!(Contatti::default().vuoti());
        assert!(!Contatti { cellulare: Some("3331234567".into()), ..Default::default() }.vuoti());
    }
}
