#!/usr/bin/env bash
# Avvia l'app installata e controlla dal suo registro che sia partita: "avvio, versione" e nessun
# "avvio non riuscito" (in quel caso l'app resta aperta su una finestra di errore, quindi non basta
# che il processo sia vivo). Senza ricette l'app non fa richieste al portale.
# Uso: prova-avvio.sh <eseguibile> [comando che lo lancia, per esempio xvfb-run -a]
set -euo pipefail

# cancella il registro dell'app: solo sulle macchine di GitHub Actions, mai sul computer di chi la usa
if [ "${CI:-}" != "true" ]; then
  echo "Solo per GitHub Actions"
  exit 2
fi

exe="$1"
shift
id="io.github.ilmondovero.cup"

case "$(uname -s)" in
  Linux*) registro="$HOME/.local/share/$id/logs/cup.log" ;;
  Darwin*) registro="$HOME/Library/Logs/$id/cup.log" ;;
  *) registro="$LOCALAPPDATA/$id/logs/cup.log" ;;
esac

vivo() {
  case "$(uname -s)" in
    Linux* | Darwin*) kill -0 "$pid" 2>/dev/null ;;
    *) tasklist //FI "IMAGENAME eq $(basename "$exe")" | grep -qi "$(basename "$exe")" ;;
  esac
}

ferma() {
  case "$(uname -s)" in
    Linux* | Darwin*)
      kill "$pid" 2>/dev/null || true
      pkill -x cup-piemonte 2>/dev/null || true # sotto xvfb-run o AppImage $pid non e' l'app
      ;;
    *) taskkill //F //IM "$(basename "$exe")" >/dev/null 2>&1 || true ;;
  esac
  sleep 2
}

rm -f "$registro"
"$@" "$exe" &
pid=$!

for _ in $(seq 1 60); do
  grep -q "avvio, versione" "$registro" 2>/dev/null && break
  sleep 1
done
sleep 10 # il tempo di aprire archivio, icona e finestra

echo "--- registro ($registro)"
cat "$registro" 2>/dev/null || echo "(non c'e')"
echo "---"

esito=0
if ! grep -q "avvio, versione" "$registro" 2>/dev/null; then
  echo "L'app non ha scritto l'avvio nel registro"
  esito=1
elif grep -q "avvio non riuscito" "$registro"; then
  echo "L'app si e' avviata con un errore"
  esito=1
elif ! vivo; then
  echo "L'app si e' chiusa da sola"
  esito=1
else
  echo "Avvio riuscito: $exe"
fi
ferma
exit $esito
