#!/usr/bin/env bash
# Prova l'aggiornamento dell'app installata, con il codice di un aggiornamento vero: la lancia con
# CUP_PROVA_AGGIORNAMENTO=<pacchetto> (la firma .sig accanto). L'app verifica la firma con la chiave che ha dentro,
# riconosce come e' installata, copia le ricette, installa il pacchetto sopra se stessa (stessa versione) e si
# riavvia. Dopo il riavvio il registro deve dire "aggiornamenti: aggiornata dalla versione X alla X".
# Uso: prova-aggiornamento.sh <versione> <eseguibile> <pacchetto> [comando che lo lancia]
set -euo pipefail

# cancella registro e stato degli aggiornamenti: solo sulle macchine di GitHub Actions
if [ "${CI:-}" != "true" ]; then
  echo "Solo per GitHub Actions"
  exit 2
fi

versione="$1"
exe="$2"
pacchetto="$3"
shift 3
id="io.github.ilmondovero.cup"
nome="cup-piemonte"

case "$(uname -s)" in
  Linux*) sistema=linux registro="$HOME/.local/share/$id/logs/cup.log" dati="$HOME/.local/share/$id" ;;
  Darwin*) sistema=macos registro="$HOME/Library/Logs/$id/cup.log" dati="$HOME/Library/Application Support/$id" ;;
  *) sistema=windows registro="$LOCALAPPDATA/$id/logs/cup.log" dati="$APPDATA/$id" ;;
esac

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

ferma
rm -f "$registro" "$dati/aggiornamenti.json"
CUP_PROVA_AGGIORNAMENTO="$pacchetto" "$@" "$exe" &

atteso="aggiornamenti: aggiornata dalla versione $versione alla $versione"
for _ in $(seq 1 240); do
  grep -q "$atteso" "$registro" 2>/dev/null && break
  grep -q "aggiornamenti: prova non riuscita" "$registro" 2>/dev/null && break
  sleep 1
done
sleep 5 # dopo il riavvio l'app deve restare aperta

echo "--- registro ($registro)"
cat "$registro" 2>/dev/null || echo "(non c'e')"
echo "---"

esito=0
if grep -q "aggiornamenti: prova non riuscita" "$registro" 2>/dev/null; then
  echo "La prova di aggiornamento non e' riuscita"
  esito=1
elif ! grep -q "aggiornamenti: installo la versione $versione" "$registro" 2>/dev/null; then
  echo "L'app non ha installato il pacchetto"
  esito=1
elif ! grep -q "$atteso" "$registro"; then
  echo "Dopo l'installazione l'app non si e' riavviata aggiornata in 4 minuti"
  esito=1
elif ! vivo; then
  echo "Dopo l'aggiornamento l'app si e' chiusa"
  esito=1
else
  echo "Aggiornamento riuscito: $pacchetto"
fi
ferma
exit $esito
