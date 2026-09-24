"""Archivio utenti: SQLite, con i dati personali cifrati (Fernet).

In chiaro restano solo l'id della chat Telegram e i campi che servono a pianificare i controlli.
Codice fiscale, NRE e tutto cio' che riguarda la prenotazione stanno nel campo `dati`, cifrato
con la chiave CUP_BOT_KEY (che non va mai messa nel repository).

    py store.py genkey     stampa una chiave nuova da mettere in CUP_BOT_KEY
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
CREATE TABLE IF NOT EXISTS utenti (
    chat_id   INTEGER PRIMARY KEY,
    stato     TEXT    NOT NULL,          -- cf | nre | sede (registrazione) | attivo | pausa
    prossimo  REAL    NOT NULL DEFAULT 0, -- epoch del prossimo controllo
    errori    INTEGER NOT NULL DEFAULT 0,
    creato    REAL    NOT NULL,          -- inizio dell'ultima (ri)registrazione
    dati      BLOB,                       -- JSON cifrato: cf, nre, stessa_sede, notificati, ultimo, ...
    coppia    TEXT UNIQUE                 -- HMAC di codice fiscale + NRE: una ricetta, un solo utente
)
"""


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
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()

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

    def get(self, chat_id):
        r = self.db.execute("SELECT * FROM utenti WHERE chat_id = ?", (chat_id,)).fetchone()
        if not r:
            return None
        return {"chat_id": r["chat_id"], "stato": r["stato"], "prossimo": r["prossimo"],
                "errori": r["errori"], "creato": r["creato"], **self._dec(r["dati"])}

    def save(self, u):
        base = ("chat_id", "stato", "prossimo", "errori", "creato")
        dati = {k: v for k, v in u.items() if k not in base}
        coppia = self.hash(f"{u['cf']}|{u['nre']}") if u.get("cf") and u.get("nre") else None
        try:
            self.db.execute(
                "INSERT INTO utenti (chat_id, stato, prossimo, errori, creato, dati, coppia) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET stato=excluded.stato, prossimo=excluded.prossimo, "
                "errori=excluded.errori, creato=excluded.creato, dati=excluded.dati, coppia=excluded.coppia",
                (u["chat_id"], u["stato"], u.get("prossimo", 0), u.get("errori", 0), u.get("creato", time.time()),
                 self.f.encrypt(json.dumps(dati).encode()), coppia))
        except sqlite3.IntegrityError:
            self.db.rollback()
            raise GiaRegistrata()
        self.db.commit()

    def new(self, chat_id):
        u = {"chat_id": chat_id, "stato": "cf", "prossimo": 0, "errori": 0, "creato": time.time()}
        self.save(u)
        return u

    def delete(self, chat_id):
        self.db.execute("DELETE FROM utenti WHERE chat_id = ?", (chat_id,))
        self.db.commit()
        self.db.execute("VACUUM")  # le pagine liberate non restano leggibili nel file

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM utenti").fetchone()[0]

    def pulizia(self, now, registrazione_s=86400, pausa_s=30 * 86400):
        """Registrazioni lasciate a meta' da piu' di un giorno e utenti in pausa da piu' di 30 giorni
        vengono cancellati. Ritorna gli id cancellati."""
        via = [r["chat_id"] for r in self.db.execute(
            "SELECT chat_id FROM utenti WHERE stato IN ('cf','nre','sede') AND creato < ?", (now - registrazione_s,))]
        for r in self.db.execute("SELECT chat_id, dati FROM utenti WHERE stato = 'pausa'").fetchall():
            if self._dec(r["dati"]).get("pausa_da", now) < now - pausa_s:
                via.append(r["chat_id"])
        for chat_id in via:
            self.db.execute("DELETE FROM utenti WHERE chat_id = ?", (chat_id,))
        self.db.commit()
        if via:
            self.db.execute("VACUUM")
        return via

    def due(self, now):
        """Utenti attivi il cui controllo e' scaduto, dal piu' in ritardo."""
        rows = self.db.execute("SELECT chat_id FROM utenti WHERE stato = 'attivo' AND prossimo <= ? ORDER BY prossimo",
                               (now,)).fetchall()
        return [r["chat_id"] for r in rows]


if __name__ == "__main__":
    if sys.argv[1:] == ["genkey"]:
        print(Fernet.generate_key().decode())
    else:
        print(__doc__)
