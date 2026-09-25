"""Archivio: SQLite, con i dati personali cifrati (Fernet).

Ogni chat Telegram puo' seguire piu' "pratiche" (una ricetta ciascuna: la propria, un familiare...).
In chiaro restano solo l'id della chat e i campi che servono a pianificare i controlli.
Codice fiscale, NRE e tutto cio' che riguarda la prenotazione stanno nel campo `dati`, cifrato
con la chiave CUP_BOT_KEY (che non va mai messa nel repository).

    python store.py genkey     stampa una chiave nuova da mettere in CUP_BOT_KEY
"""
import hashlib
import hmac
import json
import sqlite3
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS pratiche (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    stato     TEXT    NOT NULL,          -- cf | nre | nome | sede | comune (registrazione) | attivo | pausa
    prossimo  REAL    NOT NULL DEFAULT 0, -- epoch del prossimo controllo
    errori    INTEGER NOT NULL DEFAULT 0,
    creato    REAL    NOT NULL,          -- inizio dell'ultima (ri)registrazione
    dati      BLOB,                       -- JSON cifrato: cf, nre, nome, zona, auto, notificati, ultimo, ...
    coppia    TEXT UNIQUE                 -- HMAC di codice fiscale + NRE: una ricetta, una sola pratica
);
CREATE INDEX IF NOT EXISTS ix_pratiche_chat ON pratiche(chat_id);
CREATE TABLE IF NOT EXISTS metriche (
    ts       REAL NOT NULL,              -- inizio di una sessione sul portale (niente dati personali)
    durata   REAL NOT NULL,
    riuscita INTEGER NOT NULL,
    lenta    REAL,                       -- secondi della risposta piu' lenta (NULL nelle righe piu' vecchie)
    timeout  INTEGER                     -- 1 se il portale non ha risposto in tempo
);
CREATE INDEX IF NOT EXISTS ix_metriche_ts ON metriche(ts);
CREATE TABLE IF NOT EXISTS pannelli (
    chat_id    INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL           -- messaggio fissato che il bot aggiorna con lo stato delle ricette
);
"""
REGISTRAZIONE = ("cf", "nre", "nome", "sede", "comune")
BASE = ("id", "chat_id", "stato", "prossimo", "errori", "creato")


class GiaRegistrata(Exception):
    pass


class Store:
    def __init__(self, path, key):
        if not key:
            raise SystemExit("Manca CUP_BOT_KEY (genera una chiave con: python store.py genkey)")
        key = key.encode() if isinstance(key, str) else key
        self.f = Fernet(key)
        self.hkey = hashlib.sha256(b"cup-bot-hmac|" + key).digest()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)  # bot e Mini App scrivono da thread diversi
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete = ON")  # i dati cancellati vengono sovrascritti nel file
        self.db.execute("PRAGMA journal_mode = WAL")  # bot e Mini App leggono e scrivono senza bloccarsi a vicenda
        self.db.executescript(SCHEMA)
        self._migra()

    def _migra(self):
        """Versioni precedenti: metriche senza la risposta piu' lenta; una sola ricetta per chat, nella
        tabella `utenti`."""
        colonne = {r["name"] for r in self.db.execute("PRAGMA table_info(metriche)")}
        for nome, tipo in (("lenta", "REAL"), ("timeout", "INTEGER")):
            if nome not in colonne:
                self.db.execute(f"ALTER TABLE metriche ADD COLUMN {nome} {tipo}")
        self.db.commit()
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='utenti'").fetchone():
            colonne = {r["name"] for r in self.db.execute("PRAGMA table_info(utenti)")}
            creato = "creato" if "creato" in colonne else "strftime('%s','now')"
            coppia = "coppia" if "coppia" in colonne else "NULL"
            self.db.execute("INSERT INTO pratiche (chat_id, stato, prossimo, errori, creato, dati, coppia) "
                            f"SELECT chat_id, stato, prossimo, errori, {creato}, dati, {coppia} FROM utenti")
            self.db.execute("DROP TABLE utenti")
            self.db.commit()
            self._vacuum()

    def _vacuum(self):
        try:
            self.db.execute("VACUUM")
        except sqlite3.Error:
            pass  # con secure_delete i dati sono gia' sovrascritti: il VACUUM compatta soltanto

    def _dec(self, blob):
        if not blob:
            return {}
        try:
            return json.loads(self.f.decrypt(blob))
        except InvalidToken:
            raise SystemExit("CUP_BOT_KEY non corrisponde a quella usata per cifrare il database")

    def hash(self, testo):
        """HMAC con chiave: non reversibile senza CUP_BOT_KEY (per log e unicita')."""
        return hmac.new(self.hkey, str(testo).encode(), "sha256").hexdigest()

    def _riga(self, r):
        return {k: r[k] for k in BASE} | self._dec(r["dati"])

    def get(self, pid):
        r = self.db.execute("SELECT * FROM pratiche WHERE id = ?", (pid,)).fetchone()
        return self._riga(r) if r else None

    def della_chat(self, chat_id):
        return [self._riga(r) for r in self.db.execute("SELECT * FROM pratiche WHERE chat_id = ? ORDER BY id", (chat_id,))]

    def save(self, p):
        try:
            self._scrivi(p)
        except sqlite3.IntegrityError:
            self.db.rollback()
            raise GiaRegistrata()
        self.db.commit()

    def modifica(self, pid, fn):
        """Legge, modifica con fn(pratica) e riscrive in un'unica transazione: il bot (dopo un controllo
        lungo) e la Mini App non si sovrascrivono le modifiche. Ritorna la pratica aggiornata o None."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            p = self.get(pid)
            if p is None:
                self.db.rollback()
                return None
            fn(p)
            self._scrivi(p)
        except sqlite3.IntegrityError:
            self.db.rollback()
            raise GiaRegistrata()
        except BaseException:
            self.db.rollback()
            raise
        self.db.commit()
        return p

    def _scrivi(self, p):
        dati = {k: v for k, v in p.items() if k not in BASE}
        # una ricetta in pausa perche' non piu' trovata non resta "occupata"
        coppia = self.hash(f"{p['cf']}|{p['nre']}") if p.get("cf") and p.get("nre") and not p.get("libera") else None
        valori = (p["chat_id"], p["stato"], p.get("prossimo", 0), p.get("errori", 0), p.get("creato", time.time()),
                  self.f.encrypt(json.dumps(dati).encode()), coppia)
        if p.get("id"):
            self.db.execute("UPDATE pratiche SET chat_id=?, stato=?, prossimo=?, errori=?, creato=?, dati=?, coppia=? "
                            "WHERE id=?", valori + (p["id"],))
        else:
            p["id"] = self.db.execute("INSERT INTO pratiche (chat_id, stato, prossimo, errori, creato, dati, coppia) "
                                      "VALUES (?, ?, ?, ?, ?, ?, ?)", valori).lastrowid

    def new(self, chat_id, **dati):
        p = {"chat_id": chat_id, "stato": "cf", "prossimo": 0, "errori": 0, "creato": time.time(), **dati}
        self.save(p)
        return p

    def delete(self, pid):
        self.db.execute("DELETE FROM pratiche WHERE id = ?", (pid,))
        self.db.commit()
        self._vacuum()

    def pannello(self, chat_id):
        r = self.db.execute("SELECT message_id FROM pannelli WHERE chat_id = ?", (chat_id,)).fetchone()
        return r["message_id"] if r else None

    def set_pannello(self, chat_id, message_id):
        if message_id:
            self.db.execute("INSERT OR REPLACE INTO pannelli (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id))
        else:
            self.db.execute("DELETE FROM pannelli WHERE chat_id = ?", (chat_id,))
        self.db.commit()

    def delete_chat(self, chat_id):
        self.db.execute("DELETE FROM pannelli WHERE chat_id = ?", (chat_id,))
        self.db.execute("DELETE FROM pratiche WHERE chat_id = ?", (chat_id,))
        self.db.commit()
        self._vacuum()

    def chat_count(self):
        """Chat che seguono davvero almeno una ricetta (le registrazioni a meta' non occupano posti)."""
        return self.db.execute("SELECT COUNT(DISTINCT chat_id) FROM pratiche WHERE stato IN ('attivo','pausa')").fetchone()[0]

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM pratiche").fetchone()[0]

    def pulizia(self, now, registrazione_s=86400, pausa_s=30 * 86400):
        """Registrazioni lasciate a meta' da piu' di un giorno e pratiche in pausa da piu' di 30 giorni
        vengono cancellate. Ritorna [(id, chat_id)] cancellati."""
        segnaposto = ",".join("?" * len(REGISTRAZIONE))
        via = [(r["id"], r["chat_id"]) for r in self.db.execute(
            f"SELECT id, chat_id FROM pratiche WHERE stato IN ({segnaposto}) AND creato < ?",
            (*REGISTRAZIONE, now - registrazione_s))]
        for r in self.db.execute("SELECT id, chat_id, dati FROM pratiche WHERE stato = 'pausa'").fetchall():
            dati = self._dec(r["dati"])
            if "pausa_da" not in dati:  # pratiche in pausa da prima di questo campo: si parte da ora
                dati["pausa_da"] = now
                self.db.execute("UPDATE pratiche SET dati = ? WHERE id = ?",
                                (self.f.encrypt(json.dumps(dati).encode()), r["id"]))
            elif dati["pausa_da"] < now - pausa_s:
                via.append((r["id"], r["chat_id"]))
        for pid, _ in via:
            self.db.execute("DELETE FROM pratiche WHERE id = ?", (pid,))
        self.db.commit()
        if via:
            self._vacuum()
        return via

    def metrica(self, ts, durata, riuscita, lenta=None, timeout=False, tieni_giorni=7):
        self.db.execute("INSERT INTO metriche (ts, durata, riuscita, lenta, timeout) VALUES (?, ?, ?, ?, ?)",
                        (ts, durata, int(riuscita), lenta, int(timeout)))
        self.db.execute("DELETE FROM metriche WHERE ts < ?", (ts - tieni_giorni * 86400,))
        self.db.commit()

    def metriche(self, dal):
        """[(ts, durata, riuscita, lenta, timeout)] dal momento indicato, e l'ora della metrica piu' vecchia."""
        righe = [(ts, d, ok, lenta, bool(to)) for ts, d, ok, lenta, to in self.db.execute(
            "SELECT ts, durata, riuscita, lenta, timeout FROM metriche WHERE ts >= ? ORDER BY ts", (dal,))]
        prima = self.db.execute("SELECT MIN(ts) FROM metriche").fetchone()[0]
        return righe, prima

    def due(self, now):
        """Pratiche attive il cui controllo e' scaduto, dalla piu' in ritardo."""
        rows = self.db.execute("SELECT id FROM pratiche WHERE stato = 'attivo' AND prossimo <= ? ORDER BY prossimo",
                               (now,)).fetchall()
        return [r["id"] for r in rows]


if __name__ == "__main__":
    if sys.argv[1:] == ["genkey"]:
        print(Fernet.generate_key().decode())
    else:
        print(__doc__)
