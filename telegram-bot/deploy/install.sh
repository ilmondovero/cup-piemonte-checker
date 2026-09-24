#!/usr/bin/env bash
# Installa o aggiorna il bot su un server Debian/Ubuntu. Da eseguire come root,
# dopo aver clonato e letto il repository:
#   bash /opt/cup-piemonte-checker/telegram-bot/deploy/install.sh <url-del-repo> [tag-o-commit]
# Con un tag o un commit il server resta su quella versione: gli aggiornamenti li scegli tu.
set -euo pipefail

REPO="${1:?uso: install.sh <url del repository git> [tag-o-commit]}"
REF="${2:-}"
DIR=/opt/cup-piemonte-checker
ENV_FILE=/etc/cup-bot.env

apt-get update -qq
apt-get install -y -qq git python3 python3-venv

id cupbot >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin cupbot

if [ -d "$DIR/.git" ]; then
    git -C "$DIR" fetch --tags --quiet
    [ -n "$REF" ] || git -C "$DIR" pull --ff-only --quiet
else
    git clone --quiet "$REPO" "$DIR"
fi
[ -z "$REF" ] || git -C "$DIR" checkout --quiet "$REF"
echo ">> Versione: $(git -C "$DIR" log -1 --format='%h %s')"

python3 -m venv "$DIR/telegram-bot/venv"
"$DIR/telegram-bot/venv/bin/pip" install -q -r "$DIR/telegram-bot/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
    install -m 600 -o root -g root "$DIR/telegram-bot/.env.example" "$ENV_FILE"
    # la chiave si genera e si scrive dentro Python: non passa mai su una riga di comando
    "$DIR/telegram-bot/venv/bin/python" - "$ENV_FILE" <<'PY'
import re, sys
from cryptography.fernet import Fernet
p = sys.argv[1]
s = open(p).read()
open(p, "w").write(re.sub(r"(?m)^CUP_BOT_KEY=.*$", "CUP_BOT_KEY=" + Fernet.generate_key().decode(), s))
PY
    echo
    echo ">> Creato $ENV_FILE con una chiave di cifratura nuova."
    echo ">> Salva una copia di CUP_BOT_KEY in un posto sicuro, poi inserisci TELEGRAM_BOT_TOKEN:"
    echo ">>     nano $ENV_FILE"
    echo ">> e avvia con:  systemctl enable --now cup-bot"
fi

install -m 644 "$DIR/telegram-bot/deploy/cup-bot.service" /etc/systemd/system/cup-bot.service
systemctl daemon-reload
if systemctl is-enabled --quiet cup-bot 2>/dev/null; then
    systemctl restart cup-bot
    echo ">> Bot aggiornato e riavviato. Log: journalctl -u cup-bot -f"
fi
