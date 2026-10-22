#!/usr/bin/env bash
# Avvia l'app installata e controlla dal suo registro che sia partita davvero: "avvio, versione X" e poi
# "avvio completato" (archivio, icona e finestra pronti), mai "avvio non riuscito". Con un errore l'app
# resta aperta su una finestra, quindi non basta che il processo sia vivo. Senza ricette l'app non fa
# richieste al portale.
# Uso: prova-avvio.sh <versione> <eseguibile> [comando che lo lancia, per esempio xvfb-run -a]
set -euo pipefail

# cancella il registro dell'app: solo sulle macchine di GitHub Actions, mai sul computer di chi la usa
if [ "${CI:-}" != "true" ]; then
  echo "Solo per GitHub Actions"
  exit 2
fi

versione="$1"
exe="$2"
shift 2
id="io.github.ilmondovero.cup"
nome="cup-piemonte"

case "$(uname -s)" in
  Linux*) sistema=linux registro="$HOME/.local/share/$id/logs/cup.log" ;;
  Darwin*) sistema=macos registro="$HOME/Library/Logs/$id/cup.log" ;;
  *) sistema=windows registro="$LOCALAPPDATA/$id/logs/cup.log" ;;
esac

# per nome e non per pid: sotto xvfb-run o AppImage il pid lanciato non e' quello dell'app
vivo() {
  case "$sistema" in
    windows) tasklist //FI "IMAGENAME eq $nome.exe" | grep -qi "$nome.exe" ;;
    *) pgrep -x "$nome" >/dev/null ;;
  esac
}

ferma() {
  case "$sistema" in
    windows) taskkill //F //IM "$nome.exe" >/dev/null 2>&1 || true ;;
    *) pkill -x "$nome" 2>/dev/null || true ;;
  esac
  sleep 2
}

# si parte puliti: una copia gia' aperta farebbe uscire subito questa (una sola copia dell'app)
ferma
rm -f "$registro"
"$@" "$exe" &

for _ in $(seq 1 90); do
  grep -q "avvio completato" "$registro" 2>/dev/null && break
  grep -q "avvio non riuscito" "$registro" 2>/dev/null && break
  sleep 1
done
sleep 5 # l'app deve restare aperta anche dopo l'avvio

echo "--- registro ($registro)"
cat "$registro" 2>/dev/null || echo "(non c'e')"
echo "---"

esito=0
if ! grep -q "avvio, versione $versione\$" "$registro" 2>/dev/null; then
  echo "Nel registro manca l'avvio della versione $versione"
  esito=1
elif grep -q "avvio non riuscito" "$registro"; then
  echo "L'app si e' avviata con un errore"
  esito=1
elif ! grep -q "avvio completato" "$registro"; then
  echo "L'app non ha finito l'avvio in 90 secondi"
  esito=1
elif ! vivo; then
  echo "L'app si e' chiusa da sola"
  esito=1
else
  echo "Avvio riuscito: $exe"
fi
ferma
exit $esito
