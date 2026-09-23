# Spostare CUP Piemonte Checker su un'altra macchina (VPS o altro PC)

## Cosa mettere sulla chiavetta

Copia l'intera cartella `cup-piemonte-checker`, **tranne**:
- `__pycache__` (file temporanei Python, si rigenerano da soli, non servono)
- `screenshots/` (puoi svuotarla, si riempie di nuovo da sola)

**Attenzione a `config.json`**: contiene il tuo codice fiscale, il numero ricetta e il token del bot Telegram in chiaro. Trasferiscilo solo su una chiavetta che controlli tu, non lasciarlo in giro, e cancellalo dalla chiavetta una volta finito il trasferimento.

Se preferisci non portare i dati sulla chiavetta, cancella `config.json` prima di copiare (tieni solo `config.example.json`) e lo ricompili a mano sulla macchina di destinazione.

## Due modi di farlo girare, a seconda della macchina

- **PC/Mac con schermo** (es. porti tutto su un altro computer di casa): usi `gui.py` come hai fatto finora.
- **VPS / server senza schermo**: `gui.py` **non funziona** (ha bisogno di un desktop per la finestra e l'icona nella tray). Su VPS usi `checker.py`, la versione da riga di comando — fatta apposta per girare senza interfaccia.

## Installazione (uguale su entrambi)

### Windows

```powershell
cd cup-piemonte-checker
pip install -r requirements.txt
playwright install chromium
```

### Linux (VPS)

```bash
cd cup-piemonte-checker
sudo apt update && sudo apt install -y python3 python3-pip python3-venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

`--with-deps` fa installare anche le librerie di sistema che Chromium richiede su Linux (senza non parte). Su una VPS minimale la prima volta puo' scaricare/installare parecchia roba: e' normale.

## Uso su VPS (senza schermo)

Verifica prima che funzioni con un singolo controllo:

```bash
python3 checker.py --once
```

Poi fallo girare stabilmente in background con **systemd** (consigliato, si riavvia da solo se crasha o se la VPS si riavvia):

Crea `/etc/systemd/system/cup-checker.service`:

```ini
[Unit]
Description=CUP Piemonte availability checker
After=network.target

[Service]
Type=simple
WorkingDirectory=/percorso/completo/cup-piemonte-checker
ExecStart=/percorso/completo/cup-piemonte-checker/venv/bin/python3 checker.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Sostituisci `/percorso/completo/` con il percorso reale dove hai copiato la cartella sulla VPS. Poi:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cup-checker
sudo systemctl status cup-checker      # verifica che sia partito
journalctl -u cup-checker -f           # segue il log in diretta
```

Alternativa più semplice ma meno robusta: un **cron job** ogni 15 minuti con `--once` invece del loop continuo (in questo caso l'`interval_minutes` in `config.json` diventa ininfluente, lo scandisce cron):

```bash
crontab -e
```

aggiungi:

```
*/15 * * * * cd /percorso/completo/cup-piemonte-checker && /percorso/completo/cup-piemonte-checker/venv/bin/python3 checker.py --once >> cron.log 2>&1
```

## Un avvertimento sull'IP della VPS

Come discusso in chat: molte VPS (AWS, Hetzner, OVH, DigitalOcean, ecc.) hanno IP di datacenter, spesso già segnalati come "sospetti" dai sistemi anti-bot — potrebbe **peggiorare** l'affidabilità rispetto a lasciarlo girare da una rete residenziale (es. il tuo PC di casa, o un Raspberry Pi collegato al tuo router). Se lo scopo è solo avere il controllo automatico sempre attivo senza tenere il PC principale acceso, valuta anche questa alternativa prima della VPS.

## Verifica finale post-trasferimento

```bash
python3 checker.py --test-telegram
```

Deve arrivarti un messaggio Telegram di conferma: se arriva, tutto è configurato correttamente sulla nuova macchina.
