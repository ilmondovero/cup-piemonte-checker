#!/usr/bin/env bash
# Verifica le firme degli aggiornamenti con la chiave pubblica scritta nell'app (tauri.conf.json), come fara'
# l'app prima di installare: ogni pacchetto con una .sig accanto, firmata per la versione giusta. Una chiave nei
# segreti di GitHub diversa da quella nell'app renderebbe impossibile ogni aggiornamento: meglio fermarsi qui.
# Uso: verifica-firme.sh <versione> <cartella>   (serve minisign)
set -euo pipefail

versione="$1"
cartella="$2"
conf="$(dirname "$0")/../../app/desktop/tauri.conf.json"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

jq -r .plugins.updater.pubkey "$conf" | base64 -d > "$tmp/chiave.pub"
n=0
for sig in "$cartella"/*.sig; do
  pacchetto="${sig%.sig}"
  base64 -d "$sig" > "$tmp/firma"
  if ! minisign -V -q -p "$tmp/chiave.pub" -m "$pacchetto" -x "$tmp/firma"; then
    echo "Firma non valida: $(basename "$sig")"
    exit 1
  fi
  # la versione firmata (requireSignedVersion): l'ultimo campo del commento fidato
  if ! grep '^trusted comment:' "$tmp/firma" | grep -q $'\tversion:'"$versione"'$'; then
    echo "La firma di $(basename "$pacchetto") non e' per la versione $versione:"
    grep '^trusted comment:' "$tmp/firma"
    exit 1
  fi
  echo "firma valida: $(basename "$pacchetto")"
  n=$((n + 1))
done
if [ "$n" -ne 6 ]; then
  echo "Attese 6 firme (exe, msi, app.tar.gz, AppImage, deb, rpm), trovate $n"
  exit 1
fi
